"""ADES — Assay Distributed Execution System.

The controller (the Assay API process with ``ASSAY_ADES=1``) keeps the factor
registry and a leased task queue (:mod:`assay.ades.tasks`); stateless workers
(:mod:`assay.ades.worker`) hold a local copy of the QFinVerse panel, lease slices of
factors over HTTPS, score them (:mod:`assay.ades.slice`) and send back metrics and
novelty fingerprints. See ``docs/en/design/ades.md``.
"""
