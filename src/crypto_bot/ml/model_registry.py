"""Model registry: manages ML model registration and lifecycle.

Provides a central registry for ML models, enabling dynamic model
selection and switching without code changes.
"""
from __future__ import annotations

from .base import MLModel


class ModelRegistry:
    """Registry for ML models.

    Models are registered with a unique name and can be retrieved
    by that name. This enables dynamic model selection via configuration.
    """

    def __init__(self) -> None:
        self._models: dict[str, MLModel] = {}

    def register(self, name: str, model: MLModel) -> None:
        """Register an ML model with a name.

        Args:
            name: Unique name for the model.
            model: The ML model instance to register.

        Raises:
            ValueError: If a model with this name is already registered.
        """
        if name in self._models:
            raise ValueError(f"Model '{name}' is already registered")
        self._models[name] = model

    def get(self, name: str) -> MLModel | None:
        """Get an ML model by name.

        Args:
            name: Name of the model to retrieve.

        Returns:
            The ML model instance, or None if not found.
        """
        return self._models.get(name)

    def list_models(self) -> list[str]:
        """List all registered model names.

        Returns:
            List of model names.
        """
        return list(self._models.keys())

    def is_registered(self, name: str) -> bool:
        """Check if a model is registered.

        Args:
            name: Name of the model to check.

        Returns:
            True if the model is registered, False otherwise.
        """
        return name in self._models

    def unregister(self, name: str) -> None:
        """Unregister a model by name.

        Args:
            name: Name of the model to unregister.

        Raises:
            KeyError: If the model is not registered.
        """
        if name not in self._models:
            raise KeyError(f"Model '{name}' is not registered")
        del self._models[name]

    def get_ready_models(self) -> list[str]:
        """Get names of all models that are ready for predictions.

        Returns:
            List of model names that are ready.
        """
        return [
            name for name, model in self._models.items()
            if model.is_ready()
        ]

    def set_default_model(self, name: str) -> None:
        """Set the default model name.

        Note: This is stored as metadata, not enforced by the registry.
        The calling code should respect this setting.

        Args:
            name: Name of the model to use as default.

        Raises:
            ValueError: If the model is not registered.
        """
        if not self.is_registered(name):
            raise ValueError(f"Model '{name}' is not registered")
        self._default_model = name

    def get_default_model(self) -> MLModel | None:
        """Get the default model instance.

        Returns:
            The default model instance, or None if not set.
        """
        if not hasattr(self, "_default_model"):
            return None
        return self.get(self._default_model)


# Global registry instance
_global_registry = ModelRegistry()


def get_global_registry() -> ModelRegistry:
    """Get the global model registry instance."""
    return _global_registry
