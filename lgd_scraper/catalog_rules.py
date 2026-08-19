from __future__ import annotations


LOOSE_DIAMOND_CATEGORY = "Lab Grown Diamonds"
LOOSE_DIAMOND_CATEGORY_SLUG = "lab-grown-diamonds"


def assign_loose_diamond_category(product: dict) -> None:
    """Assign the explicit import category when the source leaves it empty."""

    categories = list(product.get("categories") or [])
    if categories:
        return
    categories.append(
        {
            "id": LOOSE_DIAMOND_CATEGORY_SLUG,
            "name": LOOSE_DIAMOND_CATEGORY,
            "slug": LOOSE_DIAMOND_CATEGORY_SLUG,
            "assignment_source": "product_family:loose_diamond",
        }
    )
    product["categories"] = categories
