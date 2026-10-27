"""Class map of the two-wheeler helmet model. One place, used by merge / train / harvest."""
CLASSES = ["bike", "helmet", "no-helmet", "number-plate"]      # ids 0..3 - the order of the shipped rider_helmet.pt

# names found in public datasets -> our class (None = drop the box). Lower case, '-' and '_' and ' ' are treated alike.
ALIASES = {
    "bike": "bike", "motorcycle": "bike", "motorbike": "bike", "motorcyclist": "bike", "two wheeler": "bike", "twowheeler": "bike",
    "scooter": "bike", "moped": "bike", "rider": None, "person": None, "driver": None, "pillion": None,
    "helmet": "helmet", "with helmet": "helmet", "withhelmet": "helmet", "ff helmet": "helmet", "hf helmet": "helmet",
    "wearing helmet": "helmet", "helmet on": "helmet", "good": "helmet",
    "no helmet": "no-helmet", "nohelmet": "no-helmet", "without helmet": "no-helmet", "bare head": "no-helmet", "head": "no-helmet",
    "no helmet rider": "no-helmet", "bad": "no-helmet",
    "number plate": "number-plate", "numberplate": "number-plate", "license plate": "number-plate", "licence plate": "number-plate",
    "plate": "number-plate", "lp": "number-plate",
    "invalid": None, "0": None, "cap": None, "bicycle": None, "car": None, "truck": None, "bus": None,
}


def norm(name):
    return " ".join(str(name).lower().replace("-", " ").replace("_", " ").split())


def map_name(name):
    """our class name or None; raises KeyError for a name nobody decided about (so no silent mislabelling)."""
    n = norm(name)
    if n in ALIASES:
        return ALIASES[n]
    raise KeyError(f"unknown class name {name!r}: add it to ALIASES in classes.py (to a class or to None)")
