"""Object Monitoring (marked-object monitor) - ported from the standalone monitor's src/ package.

monitor.py / matcher.py / tracker.py   Layer 1 (label-free: template + ORB/SIFT + histogram + tracker)
open_vocab.py                          Layer 2 (YOLOE open-vocabulary detector: names, people)
verifier.py                            Layer 3 (optional Grounding DINO name suggestion)
alerts.py                              alert snapshot annotation + on-video overlay
engine.py                              one engine per camera session (replaces app.py)
"""
