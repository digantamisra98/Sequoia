import torch
from torch import nn
from torchvision import models

from common.layers import ConvBlock
from config import Config
from torch.nn import Flatten
from .classifier import Classifier


class CifarClassifier(Classifier):
    def __init__(self,
                 cifar: int,
                 hparams: Classifier.HParams,
                 config: Config):
        assert cifar in {10, 100}
        self.hidden_size = hparams.hidden_size

        
        if hparams.encoder_model:
            print("Using encoder model", hparams.encoder_model.__name__,
                  "pretrained: ", hparams.pretrained_model)

            encoder = hparams.encoder_model(pretrained=hparams.pretrained_model)
            if hparams.freeze_pretrained_model:
                # Fix the parameters of the model.
                for param in encoder.parameters():
                    param.requires_grad = False
            # TODO: add this logic for other models, or use torchlayers to infer
            # the number of hidden units to use automatically.
            if hparams.pretrained_model == models.vgg16:
                encoder.classifier = nn.Linear(25088, self.hidden_size)
            else:
                raise RuntimeError(
                    f"TODO: replace the last layer of the pretrained model "
                    f"with a linear layer with an output of size "
                    f"{self.hidden_size}"
                )
        else:
            print("Using a simple convnet model")
        else:
            encoder = nn.Sequential(
                ConvBlock(3, 16, kernel_size=3, padding=1),
                ConvBlock(16, 32, kernel_size=3, padding=1),
                ConvBlock(32, 64, kernel_size=3, padding=1),
                ConvBlock(64, 128, kernel_size=3, padding=1),
                ConvBlock(128, self.hidden_size, kernel_size=3, padding=1),
            )
        classifier = nn.Sequential(
            Flatten(),
            nn.Linear(self.hidden_size, cifar),
        )
        super().__init__(
            input_shape=(3,32,32),
            num_classes=10,
            encoder=encoder,
            classifier=classifier,
            hparams=hparams,
            config=config,
        )
    
    def preprocess_inputs(self, x):
        # No special preprocessing needed.
        x = super().preprocess_inputs(x)
        if self.hparams.pretrained_model:
            x = normalize(x, [0.485, 0.456, 0.406], [0.229, 0.224, 0.225], inplace=True)
            x = torch.nn.functional.interpolate(x, size=(224, 224))
        return x 


class Cifar10Classifier(CifarClassifier):
    def __init__(self,
                 hparams: Classifier.HParams,
                 config: Config):
        super().__init__(cifar=10, hparams=hparams, config=config)


class Cifar100Classifier(CifarClassifier):
    def __init__(self,
                 hparams: Classifier.HParams,
                 config: Config):
        super().__init__(cifar=100, hparams=hparams, config=config)


def normalize(tensor, mean, std, inplace=False):
    if not inplace:
        tensor = tensor.clone()
    dtype = tensor.dtype
    mean = torch.as_tensor(mean, dtype=dtype, device=tensor.device)
    std = torch.as_tensor(std, dtype=dtype, device=tensor.device)
    tensor.sub_(mean[None, :, None, None]).div_(std[None, :, None, None])
    return tensor
