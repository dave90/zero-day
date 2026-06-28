"""BountyBench overlay package.

These modules are designed to be **copied into a BountyBench checkout** (as the
`zero_adapter` package) by `install_overlay.py`, where they plug `zero` in as a
drop-in replacement for BountyBench's `ExecutorAgent`. They use relative imports
among themselves and import the installed `zero` package; the agent/phase/workflow
modules additionally import BountyBench's own modules (`agents.*`, `phases.*`,
`workflows.*`) and therefore only import successfully *inside* a BB checkout.

`zero_driver` is the exception: it depends only on `zero.*`, so it is importable
(and unit-tested) standalone in the zero repo.
"""
