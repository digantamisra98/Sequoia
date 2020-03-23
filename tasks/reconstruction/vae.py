from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Tuple, Optional

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from tasks.auxiliary_task import AuxiliaryTask
from common.losses import LossInfo
from common.layers import DeConvBlock, Flatten, Reshape


class VAEReconstructionTask(AuxiliaryTask):
    """ Task that adds the VAE loss (reconstruction + KL divergence). 
    
    Uses the feature extractor (`encoder`) of the parent model as the encoder of
    a VAE. Contains trainable `mu`, `logvar`, and `decoder` modules, which are
    used to get the VAE loss to train the feature extractor with.    
    """
    @dataclass
    class Options(AuxiliaryTask.Options):
        """ Settings & Hyper-parameters related to the VAEReconstructionTask. """
        code_size: int = 50  # dimensions of the VAE code-space.

    def __init__(self, coefficient: float=None, name: str="vae", options: "VAEReconstructionTask.Options"=None):
        super().__init__(coefficient=coefficient, name=name, options=options)
        self.code_size = self.options.code_size  # type: ignore
        # add the rest of the VAE layers: (Mu, Sigma, and the decoder)
        self.mu     = nn.Linear(AuxiliaryTask.hidden_size, self.code_size)
        self.logvar = nn.Linear(AuxiliaryTask.hidden_size, self.code_size)
        # TODO: actually use a deconv-ish architecture for the decoder.
        # self.decoder: Optional[nn.Module] = None
        # self.decoder = get_decoder((100,))
        self.decoder = nn.Sequential(
            Reshape([self.code_size, 1, 1]),
            nn.ConvTranspose2d(self.code_size, 32, kernel_size = 4 , stride = 1),
            nn.BatchNorm2d(32),
            nn.ELU(alpha=1.0,inplace=True),
            nn.ConvTranspose2d(32, 16, kernel_size = 5, stride=2),
            nn.BatchNorm2d(16),
            nn.ELU(alpha=1.0,inplace=True),
            nn.ConvTranspose2d(16,16,kernel_size = 5, stride=2),
            nn.BatchNorm2d(16),
            nn.ELU(alpha=1.0,inplace=True),
            nn.ConvTranspose2d(16,1,kernel_size=4,stride=1),
            nn.Sigmoid(),
        )

    def forward(self, h_x: Tensor) -> Tensor:  # type: ignore
        h_x = h_x.view([h_x.shape[0], -1])
        mu, logvar = self.mu(h_x), self.logvar(h_x)
        z = self.reparameterize(mu, logvar)
        x_hat = self.decoder(z)
        return x_hat

    def reparameterize(self, mu: Tensor, logvar: Tensor) -> Tensor:
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        z = mu + eps*std
        return z

    def get_loss(self, x: Tensor, h_x: Tensor, y_pred: Tensor=None, y: Tensor=None) -> LossInfo:
        h_x = h_x.view([h_x.shape[0], -1])
        mu, logvar = self.mu(h_x), self.logvar(h_x)
        z = self.reparameterize(mu, logvar)
        x_hat = self.decoder(z)
        
        recon_loss = self.reconstruction_loss(x_hat, x)
        kl_loss = self.kl_divergence_loss(mu, logvar)
        loss = recon_loss + kl_loss
        loss_info = LossInfo(self.name)
        loss_info.total_loss = loss
        loss_info.losses["recon"] = recon_loss
        loss_info.losses["kl"] = kl_loss
        return loss_info

    def reconstruct(self, x: Tensor) -> Tensor:
        h_x = self.encode(x)
        x_hat = self.forward(h_x)
        return x_hat.view(x.shape)
    
    def generate(self, z: Tensor) -> Tensor:
        z = z.to(self.device)
        return self.forward(z)

    # Reconstruction + KL divergence losses summed over all elements and batch
    @staticmethod
    def reconstruction_loss(recon_x: Tensor, x: Tensor) -> Tensor:
        return F.binary_cross_entropy(recon_x, x.view(recon_x.shape))

    @staticmethod
    def kl_divergence_loss(mu: Tensor, logvar: Tensor) -> Tensor:
        # see Appendix B from VAE paper:
        # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
        # https://arxiv.org/abs/1312.6114
        # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
        return -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())


def get_decoder(hidden_size: torch.Size) -> nn.Module:
    print("getting decoder for code of size", hidden_size)
    batch_size = hidden_size[0]
    input_shape = [batch_size, sum(hidden_size[1:]), 1, 1]
    if len(hidden_size) == 4:
        if hidden_size[2] == hidden_size[3] == 1:
            # its of the form [b, c, 1, 1]
            height = 1
            channels = hidden_size[1]
            while channels % 2 == 0:
                channels //= 4
                height *= 2
            target_shape = [channels, height, height]
    # print("reshaped input shape:", target_shape)
    
    return nn.Sequential(
        Flatten(),
        # Reshape(target_shape=target_shape),
        DeConvBlock(in_channels=channels, out_channels=16),
        DeConvBlock(in_channels=16, out_channels=32),
        DeConvBlock(in_channels=32, out_channels=1),
        Reshape(target_shape=[1, 28, 28]),
        nn.Sigmoid(),
    )
    