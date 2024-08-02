""" Script for useful functions """

import yaml
from pathlib import Path
import torch.nn as nn
import torchaudio
import torch

def load_yaml(yaml_path: str) -> dict:
    """Function used to load the yaml content. Useful for reading configuration files or any other data stored in YAML format.

    Args:
        yaml_path (str): The absolute path to the yaml

    Returns:
        dict: The yaml content
    """
    with open(yaml_path, 'r') as file:
        try:
            yaml_content = yaml.safe_load(file)
            return yaml_content
        except yaml.YAMLError as e:
            print(e)
            return None
        

def create_exp_dir(name: str, model: str, task: str) -> str:
    """
    Function to create a unique experiment directory. Useful for organizing experiment results by model and task.

    Args:
        name (str): The base name for the experiment directory.
        task (str): The name of the task associated with the experiment.
        model (str): The name of the model associated with the experiment.

    Returns:
        str: The path to the created experiment directory.
    """
    parent_path = Path(f'runs/{model}/{task}')
    parent_path.mkdir(exist_ok=True, parents=True)
    exp_path = str(parent_path / name) + "_{:02d}"
    i = 0
    while Path(exp_path.format(i)) in list(parent_path.glob("*")):
        i += 1
    exp_path = Path(exp_path.format(i))
    exp_path.mkdir(exist_ok=True)
    return str(exp_path)





class AugmentMelSTFT(nn.Module):
    """ This class is used in order to generate the mel spectrograms for the EffAT and PaSST models
    """
    def __init__(self, n_mels=128, sr=32000, win_length=800, hopsize=320, n_fft=1024, freqm=48, timem=192,
                 fmin=0.0, fmax=None, fmin_aug_range=10, fmax_aug_range=2000):
        torch.nn.Module.__init__(self)
        # adapted from: https://github.com/CPJKU/kagglebirds2020/commit/70f8308b39011b09d41eb0f4ace5aa7d2b0e806e

        self.win_length = win_length
        self.n_mels = n_mels
        self.n_fft = n_fft
        self.sr = sr
        self.fmin = fmin
        if fmax is None:
            fmax = sr // 2 - fmax_aug_range // 2
            print(f"Warning: FMAX is None setting to {fmax} ")
        self.fmax = fmax
        self.hopsize = hopsize
        self.register_buffer('window',
                             torch.hann_window(win_length, periodic=False),
                             persistent=False)
        assert fmin_aug_range >= 1, f"fmin_aug_range={fmin_aug_range} should be >=1; 1 means no augmentation"
        assert fmax_aug_range >= 1, f"fmax_aug_range={fmax_aug_range} should be >=1; 1 means no augmentation"
        self.fmin_aug_range = fmin_aug_range
        self.fmax_aug_range = fmax_aug_range

        self.register_buffer("preemphasis_coefficient", torch.as_tensor([[[-.97, 1]]]), persistent=False)
        if freqm == 0:
            self.freqm = torch.nn.Identity()
        else:
            self.freqm = torchaudio.transforms.FrequencyMasking(freqm, iid_masks=True)
        if timem == 0:
            self.timem = torch.nn.Identity()
        else:
            self.timem = torchaudio.transforms.TimeMasking(timem, iid_masks=True)

    def forward(self, x):
        # x = nn.functional.conv1d(x.unsqueeze(1), self.preemphasis_coefficient).squeeze(1)  # Makes the mels look bad
        x = torch.stft(x, self.n_fft, hop_length=self.hopsize, win_length=self.win_length,
                       center=True, normalized=False, window=self.window, return_complex=False)
        x = (x ** 2).sum(dim=-1)  # power mag
        # GOOD ONES
        fmin = self.fmin + torch.randint(self.fmin_aug_range, (1,)).item()
        fmax = self.fmax + self.fmax_aug_range // 2 - torch.randint(self.fmax_aug_range, (1,)).item()
        
        # don't augment eval data
        if not self.training:
            fmin = self.fmin
            fmax = self.fmax

        mel_basis, _ = torchaudio.compliance.kaldi.get_mel_banks(self.n_mels,  self.n_fft, self.sr,
                                        fmin, fmax, vtln_low=100.0, vtln_high=-500., vtln_warp_factor=1.0)
        mel_basis = torch.as_tensor(torch.nn.functional.pad(mel_basis, (0, 1), mode='constant', value=0),
                                    device=x.device)
        with torch.cuda.amp.autocast(enabled=False):
            melspec = torch.matmul(mel_basis, x)

        melspec = (melspec + 0.00001).log()

        if self.training:
            melspec = self.freqm(melspec)
            melspec = self.timem(melspec)

        melspec = (melspec + 4.5) / 5.  # fast normalization

        return melspec
    