"""Local pilot tooling for document-grounded hybrid explainer videos."""

__all__ = ["PipelineError", "execute_pilot"]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    from .pilot import PipelineError, execute_pilot

    return {"PipelineError": PipelineError, "execute_pilot": execute_pilot}[name]
