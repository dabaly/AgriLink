"""Controlled values used by marketplace forms and services."""

KENYAN_COUNTIES = (
    "Baringo",
    "Bomet",
    "Bungoma",
    "Busia",
    "Elgeyo-Marakwet",
    "Embu",
    "Garissa",
    "Homa Bay",
    "Isiolo",
    "Kajiado",
    "Kakamega",
    "Kericho",
    "Kiambu",
    "Kilifi",
    "Kirinyaga",
    "Kisii",
    "Kisumu",
    "Kitui",
    "Kwale",
    "Laikipia",
    "Lamu",
    "Machakos",
    "Makueni",
    "Mandera",
    "Marsabit",
    "Meru",
    "Migori",
    "Mombasa",
    "Murang'a",
    "Nairobi",
    "Nakuru",
    "Nandi",
    "Narok",
    "Nyamira",
    "Nyandarua",
    "Nyeri",
    "Samburu",
    "Siaya",
    "Taita-Taveta",
    "Tana River",
    "Tharaka-Nithi",
    "Trans Nzoia",
    "Turkana",
    "Uasin Gishu",
    "Vihiga",
    "Wajir",
    "West Pokot",
)

INITIAL_CATEGORIES = (
    ("Crops", "crops", "Cereals, fruits, vegetables and other farm produce."),
    ("Livestock", "livestock", "Livestock and animal products."),
    ("Inputs", "inputs", "Seeds, feed, fertilizer and other farm inputs."),
    ("Equipment", "equipment", "Farm tools, machinery and equipment."),
)

MAX_IMAGES_PER_LISTING = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DIMENSION = 1600
THUMBNAIL_DIMENSION = 400
PAGE_SIZE = 12
MAX_PAGE_SIZE = 24
