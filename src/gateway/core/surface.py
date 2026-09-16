"""The shape of a management surface, as the route group that serves it declares it."""

from dataclasses import dataclass
from enum import StrEnum


class DeploymentKind(StrEnum):
    """A kind of deployment that can host a management surface.

    Hybrid is not one: its control plane is otari.ai, so a hybrid gateway hosts no surface.
    """

    STANDALONE = "standalone"
    HOSTED = "hosted"


@dataclass(frozen=True)
class Surface:
    """A management surface and the deployment kinds that host it.

    ``name`` is what the deployment bootstrap publishes and a dashboard page gates on.
    It names the route group's ``/api/v1/`` prefix, unless the group is nested under another surface's prefix.
    It is a surface and not a capability: capability is the entitlement axis, and this is the topology axis.
    Hosting a surface does not say whether a caller may use it.
    ``deployments`` defaults to every kind.
    """

    name: str
    deployments: frozenset[DeploymentKind] = frozenset(DeploymentKind)
