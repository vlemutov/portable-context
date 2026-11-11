"""Expected errors whose fixed messages are safe to return across MCP."""


class InputError(ValueError):
    """Do not include source content, credentials or arbitrary exception text."""
