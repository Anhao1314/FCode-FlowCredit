"""Audit probe scenarios: versioned code that drives a target mechanism.

A scenario is the PoC's executable half. It runs inside the constrained
replay worker against the target copy, drives REAL target code through
its public seams, and reports observed facts as plain data. The scenario
never declares a verdict — facts in, host decides.
"""
