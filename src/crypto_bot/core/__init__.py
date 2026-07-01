"""Core primitives: enums, value types and exceptions shared across the system.

Kept free of I/O and third-party trading dependencies so that strategy modules
(indicators, signal engine, scorer, risk) can import them without pulling in
the exchange/storage stack.
"""
