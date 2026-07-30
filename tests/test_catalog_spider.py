from __future__ import annotations

import html
import json

from scrapy.http import HtmlResponse, Request

from lgd_scraper.spiders.catalog import WooCommerceCatalogSpider


def response_for(url: str, body: str, status: int = 200) -> HtmlResponse:
    request = Request(url=url)
    return HtmlResponse(
        url=url,
        body=body.encode("utf-8"),
        encoding="utf-8",
        status=status,
        request=request,
    )


def test_store_api_product_normalization_converts_minor_units():
    spider = WooCommerceCatalogSpider(enrich_html=False)
    product = spider._normalize_product(
        {
            "id": 42,
            "name": "Solitaire Ring",
            "slug": "solitaire-ring",
            "permalink": "https://example.test/product/solitaire-ring/",
            "prices": {
                "currency_code": "USD",
                "currency_minor_unit": 2,
                "price": "129900",
                "regular_price": "139900",
                "sale_price": "129900",
            },
            "add_to_cart": {"text": "Select options"},
            "images": [{"src": "https://example.test/ring.jpg", "alt": "Ring"}],
        },
        "store",
    )

    assert product["price"] == "1299"
    assert product["regular_price"] == "1399"
    assert product["currency"] == "USD"
    assert product["type"] == "variable"
    assert product["media"][0]["source_url"].endswith("/ring.jpg")


def test_product_page_extracts_variations_attributes_categories_and_images():
    embedded = [
        {
            "variation_id": 501,
            "attributes": {
                "attribute_pa_metal": "14k-white-gold",
                "attribute_pa_ring-size": "7",
            },
            "display_price": 1200,
            "display_regular_price": 1300,
            "is_in_stock": True,
            "image": {
                "full_src": "https://example.test/variation-white.jpg",
                "alt": "White gold ring",
            },
        }
    ]
    page = f"""
    <html>
      <body class="single-product postid-99">
        <h1 class="product_title">Adriana Ring</h1>
        <span class="sku">LGD-99</span>
        <span class="posted_in">
          <a href="https://example.test/product-category/engagement-rings/">
            Engagement Rings
          </a>
        </span>
        <div class="woocommerce-product-gallery">
          <img data-large_image="https://example.test/ring-full.jpg"
               alt="Adriana ring" />
        </div>
        <table class="woocommerce-product-attributes">
          <tr>
            <th>Style</th>
            <td><p>Solitaire</p></td>
          </tr>
        </table>
        <form class="variations_form"
              data-product_variations="{html.escape(json.dumps(embedded), quote=True)}">
          <label for="pa_metal">Metal</label>
          <select id="pa_metal" name="attribute_pa_metal">
            <option value="">Choose</option>
            <option value="14k-white-gold">14K White Gold</option>
          </select>
          <input name="product_id" value="99" />
        </form>
      </body>
    </html>
    """
    spider = WooCommerceCatalogSpider()
    items = list(
        spider.parse_product_page(
            response_for("https://example.test/product/adriana-ring/", page)
        )
    )
    product = next(item for item in items if item["_record_type"] == "product")

    assert product["id"] == "99"
    assert product["name"] == "Adriana Ring"
    assert product["sku"] == "LGD-99"
    assert product["categories"][0]["name"] == "Engagement Rings"
    assert any(attribute["name"] == "Style" for attribute in product["attributes"])
    assert any(attribute["name"] == "Metal" for attribute in product["attributes"])
    assert product["variations"][0]["id"] == 501
    assert product["variations"][0]["attributes"]["metal"] == "14k-white-gold"
    assert any(media["role"] == "variation" for media in product["media"])


def test_sucuri_geo_block_detection():
    spider = WooCommerceCatalogSpider()
    response = response_for(
        "https://example.test/",
        "<h1>Access Denied - Sucuri Website Firewall</h1>"
        "<p>Block ID: GEO02</p>"
        "<p>Block reason: Access from your Country was disabled.</p>",
        status=403,
    )

    assert spider._is_blocked(response)
    spider._record_block(response)
    assert spider.access_blocked is True

