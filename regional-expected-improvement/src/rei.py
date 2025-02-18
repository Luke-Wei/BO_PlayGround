from __future__ import annotations

from typing import Any, Callable, Optional, Union

import torch
from torch import Tensor
from botorch.acquisition.objective import MCAcquisitionObjective, PosteriorTransform
from botorch.models.model import Model
from botorch.sampling.base import MCSampler
from botorch.utils.transforms import concatenate_pending_points, t_batch_mode_transform
from botorch.acquisition.analytic import AnalyticAcquisitionFunction, _scaled_improvement, _log_ei_helper
from botorch.acquisition.monte_carlo import MCAcquisitionFunction
from botorch.acquisition.objective import PosteriorTransform
from botorch.models.model import Model
from botorch.utils.transforms import t_batch_mode_transform

TAU_RELU = 1e-6
TAU_MAX = 1e-2

class LogRegionalExpectedImprovement(AnalyticAcquisitionFunction):

    _log: bool = True

    def __init__(
        self,
        model: Model,
        best_f: Union[float, Tensor],
        X_dev: Union[float, Tensor],
        posterior_transform: Optional[PosteriorTransform] = None,
        maximize: bool = True,
        length: float = 0.8,
        bounds: Optional[Union[float, Tensor]] = None,

    ):

        super().__init__(model=model, posterior_transform=posterior_transform)
        self.register_buffer("best_f", torch.as_tensor(best_f))
        self.maximize = maximize

        dim = X_dev.shape[1]
        self.n_region = X_dev.shape[0]
        self.X_dev = X_dev.reshape(self.n_region, 1, 1, -1)
        self.length = length
        if bounds is not None:
            self.bounds = bounds
        else:
            self.bounds = torch.stack([torch.zeros(dim), torch.ones(dim)]).to(device=self.X_dev.device, dtype=self.X_dev.dtype)

    @t_batch_mode_transform(expected_q=1)
    def forward(self, X: Tensor) -> Tensor:
        batch_shape = X.shape[0]
        q = X.shape[1]
        d = X.shape[2]

        # make N_x samples in design space
        X_min = (X - 0.5*self.length).clamp_min(self.bounds[0]).unsqueeze(0)
        X_max = (X + 0.5*self.length).clamp_max(self.bounds[1]).unsqueeze(0)
        Xs = (self.X_dev*(X_max - X_min) + X_min).reshape(-1, q, d)

        mean, sigma = self._mean_and_sigma(Xs)
        u = _scaled_improvement(mean, sigma, self.best_f, self.maximize)
        logei = _log_ei_helper(u) + sigma.log()
        logrei = torch.log(torch.exp(logei.reshape(self.n_region, batch_shape)).mean(axis=0))
        return logrei


class qRegionalExpectedImprovement(MCAcquisitionFunction):

    def __init__(
        self,
        model: Model,
        best_f: Union[float, Tensor],
        X_dev: Union[float, Tensor],
        sampler: Optional[MCSampler] = None,
        objective: Optional[MCAcquisitionObjective] = None,
        posterior_transform: Optional[PosteriorTransform] = None,
        X_pending: Optional[Tensor] = None,
        length: float = 0.8,
        bounds: Optional[Union[float, Tensor]] = None,
        **kwargs: Any,
    ) -> None:

        super().__init__(
            model=model,
            sampler=sampler,
            objective=objective,
            posterior_transform=posterior_transform,
            X_pending=X_pending,
        )
        self.register_buffer("best_f", torch.as_tensor(best_f, dtype=float))
        dim = X_dev.shape[1]
        self.n_region = X_dev.shape[0]
        self.X_dev = X_dev.reshape(self.n_region, 1, 1, -1)
        self.length = length
        if bounds is not None:
            self.bounds = bounds
        else:
            self.bounds = torch.stack([torch.zeros(dim), torch.ones(dim)]).to(device=self.X_dev.device, dtype=self.X_dev.dtype)


    @concatenate_pending_points
    @t_batch_mode_transform()
    def forward(self, X: Tensor) -> Tensor:
        batch_shape = X.shape[0]
        q = X.shape[1]
        d = X.shape[2]

        # make N_x samples in design space
        X_min = (X - 0.5*self.length).clamp_min(self.bounds[0]).unsqueeze(0)
        X_max = (X + 0.5*self.length).clamp_max(self.bounds[1]).unsqueeze(0)
        Xs = (self.X_dev*(X_max - X_min) + X_min).reshape(-1, q, d)

        posterior = self.model.posterior(
            X=Xs, posterior_transform=self.posterior_transform
        )
        samples = self.get_posterior_samples(posterior)
        obj = self.objective(samples, X=Xs)
        obj = (obj - self.best_f.unsqueeze(-1).to(obj)).clamp_min(0).reshape(-1, self.n_region, batch_shape, q)
        q_rei = obj.max(dim=-1)[0].mean(dim=(0,1))
        return q_rei


