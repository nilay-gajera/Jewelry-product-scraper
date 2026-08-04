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
            "variation_gallery_images": [
                {"src": "https://example.test/variation-side.jpg", "alt": "Side"},
                {"src": "https://example.test/variation-hand.jpg", "alt": "On hand"},
            ],
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
    assert len(product["variations"][0]["gallery"]) == 2
    assert sum(
        media["role"] == "variation_gallery" for media in product["media"]
    ) == 2


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


def test_variation_gallery_reads_store_extensions_and_preserves_attachment_ids():
    spider = WooCommerceCatalogSpider()
    variation = spider._normalize_variation(
        {
            "id": 700,
            "attributes": {"attribute_pa_metal": "platinum"},
            "image": {"src": "https://example.test/featured.jpg"},
            "extensions": {
                "vendor-gallery": {
                    "variation_gallery_media": [
                        {
                            "image_id": 901,
                            "gallery_image_src": "https://example.test/side.jpg",
                        },
                        {"attachment_id": 902},
                    ],
                    "gallery_image_ids": "905,906",
                }
            },
            "meta_data": [
                {"key": "_woo_variation_gallery", "value": "903,904"}
            ],
        },
        "Platinum Ring",
        "store",
    )

    assert variation["gallery"] == [
        {
            "source_url": "https://example.test/side.jpg",
            "alt": None,
            "width": None,
            "height": None,
            "position": 0,
        }
    ]
    assert variation["gallery_image_ids"] == [
        "903",
        "904",
        "901",
        "902",
        "905",
        "906",
    ]


def test_json_ld_product_group_is_preserved_as_variation_fallback():
    structured = {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "name": "Adriana Oval Solitaire Ring",
        "description": "An oval solitaire with a low-profile basket.",
        "category": "Engagement Rings",
        "hasVariant": [
            {
                "@type": "Product",
                "productID": "LGDOV-44-WG",
                "name": "Adriana — White Gold",
                "sku": "LGDOV-44-WG",
                "material": "14K White Gold",
                "size": "7",
                "image": [
                    "https://example.test/white-front.jpg",
                    "https://example.test/white-side.jpg",
                ],
                "offers": {
                    "price": "1299",
                    "priceCurrency": "USD",
                    "availability": "https://schema.org/InStock",
                },
            },
            {
                "@type": "Product",
                "productID": "LGDOV-44-YG",
                "name": "Adriana — Yellow Gold",
                "sku": "LGDOV-44-YG",
                "material": "18K Yellow Gold",
                "offers": {
                    "price": "1499",
                    "priceCurrency": "USD",
                    "availability": "https://schema.org/OutOfStock",
                },
            },
        ],
    }
    page = f"""
    <html><head>
      <meta property="og:title" content="Adriana Oval Solitaire Ring" />
      <meta property="product:brand" content="Loose Grown Diamond" />
      <script type="application/ld+json">{json.dumps(structured)}</script>
    </head><body class="single-product postid-44">
      <main><h1>Adriana Oval Solitaire Ring</h1></main>
      <div id="product-description"><p>Detailed custom-theme description.</p></div>
      <span itemprop="sku" content="LGDOV-44"></span>
      <span class="tagged_as"><a rel="tag" href="/product-tag/solitaire/">Solitaire</a></span>
    </body></html>
    """
    spider = WooCommerceCatalogSpider()
    items = list(
        spider.parse_product_page(
            response_for("https://example.test/product/adriana/", page)
        )
    )
    variation_request = next(item for item in items if isinstance(item, Request))
    product = variation_request.meta["product"]

    assert product["name"] == "Adriana Oval Solitaire Ring"
    assert product["sku"] == "LGDOV-44"
    assert product["description"] == "Detailed custom-theme description."
    assert product["tags"][0]["name"] == "Solitaire"
    assert product["brands"][0]["name"] == "Loose Grown Diamond"
    assert product["type"] == "variable"
    assert len(product["structured_variations"]) == 2
    assert product["structured_variations"][0]["gallery"][0][
        "source_url"
    ].endswith("white-side.jpg")

    failed_request = Request(
        variation_request.url,
        meta={
            "api_kind": "store",
            "page": 1,
            "product": product,
            "variations": [],
        },
    )
    failed_response = HtmlResponse(
        url=failed_request.url,
        status=403,
        body=b"forbidden",
        encoding="utf-8",
        request=failed_request,
    )
    fallback_items = list(spider.parse_store_variations(failed_response))
    fallback_product = next(
        item for item in fallback_items if item.get("_record_type") == "product"
    )
    assert fallback_product["variation_source_fallback"] == "json_ld"
    assert len(fallback_product["variations"]) == 2
    assert sum(
        media["role"] == "variation_gallery"
        for media in fallback_product["media"]
    ) == 1
