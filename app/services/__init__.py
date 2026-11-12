"""Application services orchestration (ingestion, classification, tax computation).

Tax math belongs here, and it must read parameters from ``tax_year_params`` /
``tax_profiles`` rows - never from module-level constants.
"""
