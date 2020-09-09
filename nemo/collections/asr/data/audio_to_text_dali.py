# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import nvidia.dali as dali
from nvidia.dali.pipeline import Pipeline
from nvidia.dali.plugin.pytorch import DALIGenericIterator as DALIPytorchIterator
from nemo.utils.decorators import experimental
from omegaconf import DictConfig
import numpy as np
import math
import torch

__all__ = [
    'AudioToCharDALIDataset',
]

@experimental
class AudioToCharDALIDataset(DALIPytorchIterator):
    """
    NVIDIA DALI pipeline that loads tensors via one or more manifest files where each line containing a sample descriptor in JSON,
    including audio files, transcripts, and durations (in seconds).
    Here's an example:
    {"audio_filepath": "/path/to/audio.wav", "text_filepath": "/path/to/audio.txt", "duration": 23.147}
    ...
    {"audio_filepath": "/path/to/audio.wav", "text": "the transcription", "offset": 301.75, "duration": 0.82, "utt":
    "utterance_id", "ctm_utt": "en_4156", "side": "A"}
    Args:
        manifest_filepath: Path to manifest file with the format described above. Can be comma-separated paths.
        labels: String containing all the possible characters to map to.
        sample_rate (int): Sample rate to resample loaded audio to.
        batch_size (int): Number of samples in a batch.
        num_threads (int): Number of CPU processing threads to be created by the DALI pipeline.
        trim_silence (bool): If True, it will extract the nonsilent region of the loaded audio signal.
        min_duration (float): Determines the minimum allowed duration, in seconds, of the loaded audio files.
        max_duration (float): Determines the maximum allowed duration, in seconds, of the loaded audio files.
        shuffle (bool): If set to True, the dataset will shuffled after loading.
        device (str): Determines the device type to be used for preprocessing. Allowed values are: 'cpu', 'gpu'.
        device_id (int): Index of the GPU to be used. Only applicable when device == 'gpu'. Defaults to 0.
        global_rank (int): Worker rank, used for partitioning shards. Defaults to 0.
        world_size (int): Total number of processes, used for partitioning shards. Defaults to 0.
        preprocessor_cfg (DictConfig): Preprocessor configuration
    """

    def __init__(
        self,
        manifest_filepath: str,
        labels=None,
        sample_rate: int = 16000,
        batch_size: int = 32,
        num_threads: int = 4,
        trim_silence: bool = False,
        min_duration: float = 0.0,
        max_duration: float = 0.0,
        shuffle: bool = True,
        device: str = 'gpu',
        device_id: int = 0,
        global_rank: int = 0,
        world_size: int = 0,
        preprocessor_cfg: DictConfig = None):

        self.batch_size = batch_size  # Used by NeMo

        self.device = device
        self.device_id = device_id

        if world_size > 1:
            self.shard_id = global_rank
            self.num_shards = world_size
        else:
            self.shard_id = None
            self.num_shards = None

        self.labels = labels
        if self.labels is None:
            raise ValueError(f"{self} expects a labels  parameter.")

        assert self.labels is not None

        self.label2id, self.id2label = {}, {}
        for label_id, label in enumerate(self.labels):
            self.label2id[ord(label)] = label_id
            self.id2label[label_id] = ord(label)
        self.label2id_keys = [k for k in self.label2id.keys()]
        self.label2id_values = [float(self.label2id[k]) for k in self.label2id_keys]

        self.pipes = [Pipeline(batch_size=batch_size, num_threads=num_threads, device_id=self.device_id,
                               exec_async=True, exec_pipelined=True)]

        has_preprocessor = preprocessor_cfg is not None
        if has_preprocessor:
            if preprocessor_cfg.cls == "nemo.collections.asr.modules.AudioToMelSpectrogramPreprocessor":
                feature_type = "mel_spectrogram"
            elif preprocessor_cfg.cls == "nemo.collections.asr.modules.AudioToMFCCPreprocessor":
                feature_type = "mfcc"
            else:
                assert False, "Preprocessor {} not supported".format(preprocessor_cfg.cls)

            # Default values taken from AudioToMelSpectrogramPreprocessor
            params = preprocessor_cfg.params
            self.dither = params['dither'] if 'dither' in params else 1e-5
            self.preemph = params['preemph'] if 'preemph' in params else 0.97
            self.window_size_sec = params['window_size'] if 'window_size' in params else 0.02
            self.window_stride_sec = params['window_stride'] if 'window_stride' in params else 0.01
            self.sample_rate = params['sample_rate'] if 'sample_rate' in params else sample_rate
            self.window_size = int(self.window_size_sec * self.sample_rate)
            self.window_stride = int(self.window_size_sec * self.sample_rate)

            normalize = params['normalize'] if 'normalize' in params else 'per_feature'
            if normalize == 'per_feature':  # Each freq channel independently
                self.normalization_axes = (1, )
            elif normalize == 'all_features':
                self.normalization_axes = (0, 1)
            else:
                raise ValueError(
                    f"{self} received {normalize} for the "
                    f"normalize parameter. It must be either 'per_feature' or "
                    f"'all_features'."
                )

            self.window = None
            window_name = params['window'] if 'window' in params else None
            torch_windows = {
                'hamming': torch.hamming_window,
                'blackman': torch.blackman_window,
                'bartlett': torch.bartlett_window,
            }
            if window_name is None or window_name == 'hann':
                self.window = None   # Hann is DALI's default
            elif window_name == 'ones':
                self.window = torch.ones(self.window_size)
            else:
                try:
                    window_fn = torch_windows.get(window_name, None)
                    self.window = window_fn(self.window_size, periodic=False)
                except:
                    raise ValueError(
                        f"{self} received {window_name} for the "
                        f"window parameter. It must be one of: ('hann', 'ones', 'hamming', "
                        f"'blackman', 'bartlett', None). None is equivalent to 'hann'."
                    )

            self.n_fft = params['n_fft'] if 'n_fft' in params else None  # None means default
            self.n_mels = params['n_mels'] if 'n_mels' in params else 64
            self.n_mfcc = params['n_mfcc'] if 'n_mfcc' in params else 64

            features = params['features'] if 'features' in params else 0
            if features > 0:
                if feature_type == 'mel_spectrogram':
                    self.n_mels = features
                elif feature_type == 'mfcc':
                    self.n_mfcc = features

            # TODO Implement frame splicing
            if 'frame_splicing' in params:
                assert params['frame_splicing'] == 1, "Frame splicing is not implemented"

            self.freq_low = params['lowfreq'] if 'lowfreq' in params else 0.0
            self.freq_high = params['highfreq'] if 'highfreq' in params else self.sample_rate / 2.0
            self.log_features = params['log'] if 'log' in params else True

            # We want to avoid taking the log of zero
            # There are two options: either adding or clamping to a small value

            self.log_zero_guard_type = params['log_zero_guard_type'] if 'log_zero_guard_type' in params else 'add'
            if self.log_zero_guard_type not in ["add", "clamp"]:
                raise ValueError(
                    f"{self} received {self.log_zero_guard_type} for the "
                    f"log_zero_guard_type parameter. It must be either 'add' or "
                    f"'clamp'."
                )

            self.log_zero_guard_value = params['log_zero_guard_value'] if 'log_zero_guard_value' in params else 1e-05
            if isinstance(self.log_zero_guard_value, str):
                if self.log_zero_guard_value == "tiny":
                    self.log_zero_guard_value = torch.finfo(torch.float32).tiny
                elif self.log_zero_guard_value == "eps":
                    self.log_zero_guard_value = torch.finfo(torch.float32).eps
                else:
                    raise ValueError(
                        f"{self} received {self.log_zero_guard_value} for the "
                        f"log_zero_guard_type parameter. It must be either a "
                        f"number, 'tiny', or 'eps'"
                    )

            self.mag_power = params['mag_power'] if 'mag_power' in params else 2
            if self.mag_power != 1.0 and self.mag_power != 2.0:
                raise ValueError(
                    f"{self} received {self.mag_power} for the "
                    f"mag_power parameter. It must be either 1.0 or 2.0."
                )

            self.pad_to = params['pad_to'] if 'pad_to' in params else 16
            self.pad_value = params['pad_value'] if 'pad_value' in params else 0.0

        for pipe in self.pipes:
            with pipe:
                # TODO implement offset(?)
                audio, transcript = dali.fn.nemo_asr_reader(name="Reader", manifest_filepaths = manifest_filepath.split(','),
                                                            dtype = dali.types.FLOAT, downmix = True, sample_rate=float(self.sample_rate),
                                                            min_duration=min_duration, max_duration=max_duration,
                                                            read_sample_rate=False, read_text=True, random_shuffle=shuffle,
                                                            shard_id=self.shard_id, num_shards=self.num_shards)

                transcript_len = dali.fn.shapes(dali.fn.reshape(transcript, shape=[-1]))
                transcript = dali.fn.pad(transcript)
                transcript = dali.fn.lookup_table(transcript, dtype=dali.types.INT64, keys=self.label2id_keys, values=self.label2id_values)
                if self.device == 'gpu':
                    transcript = transcript.gpu()
                    transcript_len = transcript_len.gpu()

                # Extract nonsilent region, if necessary
                if trim_silence:
                    # Need to extract non-silent region before moving to the GPU
                    roi_start, roi_len = dali.fn.nonsilent_region(audio, cutoff_db=-60)
                    audio = audio.gpu() if self.device == 'gpu' else audio
                    audio = dali.fn.slice(
                        audio, roi_start, roi_len, normalized_anchor=False, normalized_shape=False, axes=[0]
                    )
                else:
                    audio = audio.gpu() if self.device == 'gpu' else audio

                if not has_preprocessor:
                    # No preprocessing, the output is the audio signal
                    audio = dali.fn.pad(audio)
                    audio_len = dali.fn.shapes(dali.fn.reshape(audio, shape=[-1]))
                    pipe.set_outputs(audio, audio_len, transcript, transcript_len)
                else:
                    # Additive gaussian noise (dither)
                    if self.dither > 0.0:
                        gaussian_noise = dali.fn.normal_distribution(device=self.device)
                        audio = audio + self.dither * gaussian_noise

                    # Preemphasis filter
                    if self.preemph > 0.0:
                        audio = dali.fn.preemphasis_filter(audio, preemph_coeff=self.preemph)

                    # Power spectrogram
                    spec = dali.fn.spectrogram(
                        audio,
                        nfft=self.n_fft,
                        window_length=self.window_size,
                        window_step=self.window_stride
                    )

                    if feature_type == 'mel_spectrogram' or feature_type == 'mfcc':
                        # Spectrogram to Mel Spectrogram
                        spec = dali.fn.mel_filter_bank(
                            spec, sample_rate=self.sample_rate, nfilter=self.n_mels, normalize=True,
                            freq_low=self.freq_low, freq_high=self.freq_high
                        )
                        # Mel Spectrogram to MFCC
                        if feature_type == 'mfcc':
                            spec = dali.fn.mfcc(spec, n_mffc=self.n_mfcc)

                    # Logarithm
                    if self.log_zero_guard_type == 'add':
                        spec = spec + self.log_zero_guard_value

                    spec = dali.fn.to_decibels(
                        spec, multiplier=math.log(10), reference=1.0, cutoff_db=math.log(self.log_zero_guard_value)
                    )

                    # Normalization
                    spec = dali.fn.normalize(
                        spec, axes=self.normalization_axes
                    )

                    # Extracting the length of the spectrogram
                    shape_start = dali.types.Constant(np.array([1], dtype=np.float32), device='cpu')
                    shape_len = dali.types.Constant(np.array([1], dtype=np.float32), device='cpu')
                    spec_len = dali.fn.slice(
                        dali.fn.shapes(spec), shape_start, shape_len, normalized_anchor=False, normalized_shape=False, axes=(0,)
                    )

                    # Pads feature dimension to be a multiple of `pad_to` and the temporal dimension to be as big as the largest sample (shape -1)
                    spec = dali.fn.pad(
                        spec, fill_value=self.pad_value, axes=(0, 1), align=(self.pad_to, 1), shape=(1, -1)
                    )

                    pipe.set_outputs(spec, spec_len, transcript, transcript_len)

            # Building DALI pipeline
            pipe.build()

        # TODO come up with a better solution
        class DummyDataset:
            def __init__(self, parent):
                self.parent = parent
            def __len__(self):
                return self.parent.size
        self.dataset = DummyDataset(self)  # Used by NeMo

        if has_preprocessor:
            output_names = ['processed_signal', 'processed_signal_len', 'transcript', 'transcript_len']
        else:
            output_names = ['audio', 'audio_len', 'transcript', 'transcript_len']

        super(AudioToCharDALIDataset, self).__init__(self.pipes,
                                                     output_map=output_names,
                                                     reader_name="Reader",
                                                     fill_last_batch=True, dynamic_shape=True, auto_reset=True)
