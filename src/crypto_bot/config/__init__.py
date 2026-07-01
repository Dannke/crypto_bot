"""Configuration layer.

`schemas.py` defines the pydantic contract for the YAML/ENV configuration.
`settings.py` (added in stage 2) loads and merges .env + YAML into a validated
`Settings` instance.
"""
