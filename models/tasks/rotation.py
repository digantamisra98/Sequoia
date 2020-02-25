from abc import ABC, abstractmethod
from typing import Any, Tuple

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from models.bases import Model, AuxiliaryTask


def rotate(x: Tensor, angle: float) -> Tensor:
    raise NotImplementedError("TODO")


class Rotation(AuxiliaryTask):
    def get_loss(self,
                 x: Tensor,
                 h_x: Tensor,
                 y_pred: Tensor,
                 y: Tensor=None) -> Tensor:
        raise NotImplementedError("TODO")