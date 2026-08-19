from __future__ import annotations

import html
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from scrapy.http import HtmlResponse, Request

from lgd_scraper.pipelines import CatalogMediaPipeline
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


def test_store_api_product_name_decodes_html_entities():
    spider = WooCommerceCatalogSpider(enrich_html=False)

    product = spider._normalize_product(
        {"id": 42, "name": "Ali&#8217;i Ring"}, "store"
    )

    assert product["name"] == "Ali’i Ring"


def test_loose_diamond_is_identified_without_inventing_attributes():
    spider = WooCommerceCatalogSpider(enrich_html=True)

    product = spider._normalize_product(
        {
            "id": 2304165,
            "name": "Asscher Cut 0.91 Carat D Color VVS2 Clarity Lab Grown Diamond",
            "permalink": "https://example.test/product/legacy-diamond/",
            "attributes": [],
        },
        "store",
    )

    assert product["product_family"] == "loose_diamond"
    assert product["categories"] == [
        {
            "id": "lab-grown-diamonds",
            "name": "Lab Grown Diamonds",
            "slug": "lab-grown-diamonds",
            "assignment_source": "product_family:loose_diamond",
        }
    ]
    assert product["attributes"] == []
    assert product["html_enrichment"]["status"] == "skipped"
    assert product["html_enrichment"]["reason"] == "run_separate_catalog_enrichment"
    assert spider._should_enrich_product(product) is False


def test_media_paths_are_shared_by_source_url_not_misleading_product_id():
    source_url = "https://example.test/wp-content/uploads/asscher.png"
    first = Request(source_url, meta={"catalog_product_id": "one"})
    second = Request(source_url, meta={"catalog_product_id": "two"})

    first_path = CatalogMediaPipeline.file_path(None, first)
    second_path = CatalogMediaPipeline.file_path(None, second)

    assert first_path == second_path
    assert first_path.startswith("shared/")


def test_selected_categories_are_sent_to_store_api_and_products_are_deduplicated():
    spider = WooCommerceCatalogSpider(
        base_url="https://example.test/",
        category_ids="11,12,11",
        enrich_html=False,
    )
    requests = list(spider.start_requests())
    products_request = next(
        request for request in requests if request.callback == spider.parse_products_api
    )
    query = parse_qs(urlparse(products_request.url).query)

    assert spider.category_ids == ["11", "12"]
    assert query["category"] == ["11,12"]
    assert query["category_operator"] == ["in"]

    raw = {
        "id": 42,
        "name": "Ring",
        "permalink": "https://example.test/product/ring/",
        "prices": {"price": "100", "currency_minor_unit": 2},
    }
    first_request = Request(
        products_request.url,
        meta={"api_kind": "store", "page": 1},
    )
    first_response = HtmlResponse(
        url=first_request.url,
        body=json.dumps([raw]).encode(),
        encoding="utf-8",
        request=first_request,
    )
    duplicate_request = Request(
        products_request.url,
        meta={"api_kind": "store", "page": 1},
    )
    duplicate_response = HtmlResponse(
        url=duplicate_request.url,
        body=json.dumps([raw]).encode(),
        encoding="utf-8",
        request=duplicate_request,
    )

    assert len([item for item in spider.parse_products_api(first_response) if isinstance(item, dict) and item.get("_record_type") == "product"]) == 1
    assert len([item for item in spider.parse_products_api(duplicate_response) if isinstance(item, dict) and item.get("_record_type") == "product"]) == 0


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


def test_live_inline_product_variations_preserve_wvg_gallery_and_dynamic_details():
    inline_variations = [
        {
            "variation_id": 1016198,
            "attributes": {"attribute_pa_metal": "14k-white-gold"},
            "display_price": 990,
            "display_regular_price": 1090,
            "is_in_stock": True,
            "image": {
                "full_src": "https://cdn.example.test/white-front.jpg",
                "alt": "White gold ring",
            },
            "wvg_images": [
                {"src": "https://cdn.example.test/white-side.jpg", "alt": "Side"},
                {"src": "https://cdn.example.test/white-hand.jpg", "alt": "Hand"},
            ],
            "section1data": [
                "Metal: 14K White Gold",
                "Setting Profile: Tulip Basket",
                "Band Width: 1.80mm*",
            ],
        }
    ]
    structured = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Lotus Heart Solitaire Engagement Ring",
        "sku": "LGDHE-20",
        "category": "Engagement Rings",
    }
    page = f"""
    <html><head>
      <script type="application/ld+json">{json.dumps(structured)}</script>
    </head><body class="single-product postid-1016196">
      <h1 class="product_title">Lotus Heart Solitaire Engagement Ring</h1>
      <form class="variations_form custom-variations-form" data-product_id="1016196">
        <label for="pa_metal">Metal</label>
        <select id="pa_metal" name="attribute_pa_metal">
          <option value="14k-white-gold">14K White Gold</option>
        </select>
      </form>
      <script>var productVariations = {json.dumps(inline_variations)};</script>
    </body></html>
    """

    spider = WooCommerceCatalogSpider()
    items = list(
        spider.parse_product_page(
            response_for("https://example.test/product/lotus-heart/", page)
        )
    )
    product = next(item for item in items if item.get("_record_type") == "product")
    variation = product["variations"][0]

    assert product["categories"][0]["name"] == "Engagement Rings"
    assert product["html_enrichment"]["status"] == "complete"
    assert variation["source"] == "html_inline_product_variations"
    assert variation["attributes"] == {"metal": "14k-white-gold"}
    assert [item["source_url"] for item in variation["gallery"]] == [
        "https://cdn.example.test/white-side.jpg",
        "https://cdn.example.test/white-hand.jpg",
    ]
    assert variation["details"]["setting_profile"]["value"] == "Tulip Basket"
    assert variation["details"]["band_width"]["value"] == "1.80mm*"


def test_live_diamond_grid_preserves_every_dynamic_field_and_description():
    page = """
    <html><body class="single-product postid-2301663">
      <h1>1.96 Carat Marquise Cut Lab Grown Diamond</h1>
      <div class="diamond-details-grid">
        <div class="dd-item"><div class="dd-label">Carat</div><div class="dd-value fw-bold">1.96</div></div>
        <div class="dd-item"><div class="dd-label">L x W x H</div><div class="dd-value fw-bold">4.05 x 12.49 x 6.50</div></div>
        <div class="dd-item"><div class="dd-label">Cut</div><div class="dd-value"><div class="fw-bold">Ideal</div><div class="small">Maximum fire.</div></div></div>
        <div class="dd-item"><div class="dd-label">Polished</div><div class="dd-value fw-bold">Excellent</div></div>
        <div class="dd-item"><div class="dd-label">Future Source Field</div><div class="dd-value fw-bold">Dynamic value</div></div>
      </div>
    </body></html>
    """
    product = {
        "_record_type": "product",
        "id": "2301663",
        "name": "Marquise Cut 1.96 Carat D Color IF Clarity Lab Grown Diamond",
        "product_family": "loose_diamond",
        "type": "simple",
        "attributes": [],
        "media": [],
        "variations": [],
    }

    spider = WooCommerceCatalogSpider()
    items = list(
        spider.parse_product_page(
            response_for("https://example.test/diamond/LGD14-12972/", page),
            product,
        )
    )
    completed = next(item for item in items if item.get("_record_type") == "product")
    details = completed["diamond_details"]

    assert details["carat"] == 1.96
    assert details["polish"] == "Excellent"
    assert details["size_mm"]["raw"] == "4.05 x 12.49 x 6.50"
    assert details["fields"]["cut"] == {
        "label": "Cut",
        "value": "Ideal",
        "description": "Maximum fire.",
    }
    assert details["fields"]["future_source_field"]["value"] == "Dynamic value"


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


def test_sucuri_geo_block_stops_the_run_before_saved_products_are_rewritten():
    closed = []
    spider = WooCommerceCatalogSpider(enrichment_mode=True)
    spider.crawler = SimpleNamespace(
        engine=SimpleNamespace(
            close_spider=lambda active_spider, reason: closed.append(
                (active_spider, reason)
            )
        )
    )
    product = {
        "_record_type": "product",
        "id": "42",
        "name": "Saved diamond",
        "type": "simple",
        "media": [],
        "variations": [],
    }
    response = response_for(
        "https://example.test/diamond/42/",
        "<h1>Access Denied - Sucuri Website Firewall</h1>"
        "<p>Block ID: GEO02</p>",
        status=403,
    )

    emitted = list(spider.parse_product_page(response, product))

    assert [item["_record_type"] for item in emitted] == ["diagnostic"]
    assert spider.access_blocked is True
    assert closed == [(spider, "access_blocked")]


def test_sucuri_cookie_challenge_is_reported_as_blocked():
    spider = WooCommerceCatalogSpider()
    response = response_for(
        "https://example.test/wp-json/wc/store/v1/products",
        "<script>var sucuri_cloudproxy_js = 'challenge';</script>",
        status=307,
    )

    assert spider._is_blocked(response)


def test_api_product_survives_missing_optional_html_page_without_diagnostic():
    spider = WooCommerceCatalogSpider()
    api_product = {
        "_record_type": "product",
        "id": "800605728",
        "name": "Round Cut Lab Diamond",
        "type": "simple",
        "source": "store",
        "media": [{"source_url": "https://example.test/diamond.jpg"}],
        "variations": [],
    }
    items = list(
        spider.parse_product_page(
            response_for(
                "https://example.test/diamond/800605728/", "Not found", status=404
            ),
            api_product,
        )
    )

    assert len(items) == 1
    assert items[0]["_record_type"] == "product"
    assert items[0]["html_enrichment"]["http_status"] == 404


def test_product_limit_is_enforced_for_resumed_detail_requests():
    spider = WooCommerceCatalogSpider(max_products=2)
    products = []

    for product_id in ("1", "2", "3"):
        products.extend(
            spider.parse_product_page(
                response_for(
                    f"https://example.test/diamond/{product_id}/",
                    "Not found",
                    status=404,
                ),
                {
                    "_record_type": "product",
                    "id": product_id,
                    "name": f"Diamond {product_id}",
                    "type": "simple",
                    "media": [],
                    "variations": [],
                },
            )
        )

    assert [product["id"] for product in products] == ["1", "2"]
    assert spider.products_emitted == 2


def test_checkpoint_resume_skips_existing_ids_and_emits_only_new_products(tmp_path):
    database = tmp_path / "catalog.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT)")
    connection.execute(
        "INSERT INTO products VALUES ('42', ?)",
        (json.dumps({"id": 42, "name": "Already stored", "attributes": []}),),
    )
    connection.commit()
    connection.close()
    spider = WooCommerceCatalogSpider(
        output_dir=str(tmp_path), resume_existing=True, enrich_html=False
    )
    request = Request(
        "https://example.test/wp-json/wc/store/v1/products?per_page=100&page=1",
        meta={"api_kind": "store", "page": 1},
    )
    response = HtmlResponse(
        url=request.url,
        body=json.dumps(
            [
                {"id": 42, "name": "Already stored"},
                {"id": 43, "name": "New product"},
            ]
        ).encode(),
        encoding="utf-8",
        request=request,
    )

    products = [
        item
        for item in spider.parse_products_api(response)
        if isinstance(item, dict) and item.get("_record_type") == "product"
    ]

    assert spider.existing_product_ids == {"42"}
    assert spider.refresh_product_ids == set()
    assert [str(product["id"]) for product in products] == ["43"]
    assert spider.products_scheduled == 1


def test_checkpoint_resume_refreshes_legacy_diamonds_once(tmp_path):
    database = tmp_path / "catalog.sqlite"
    name = "Asscher Cut 0.91 Carat D Color VVS2 Clarity Lab Grown Diamond"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT)")
    connection.execute(
        "INSERT INTO products VALUES ('42', ?)",
        (json.dumps({"id": 42, "name": name, "attributes": []}),),
    )
    connection.commit()
    connection.close()
    spider = WooCommerceCatalogSpider(
        output_dir=str(tmp_path), resume_existing=True, enrich_html=True
    )
    request = Request(
        "https://example.test/wp-json/wc/store/v1/products?per_page=100&page=1",
        meta={"api_kind": "store", "page": 1},
    )
    response = HtmlResponse(
        url=request.url,
        body=json.dumps([{"id": 42, "name": name}]).encode(),
        encoding="utf-8",
        request=request,
    )

    products = [
        item
        for item in spider.parse_products_api(response)
        if isinstance(item, dict) and item.get("_record_type") == "product"
    ]

    assert spider.refresh_product_ids == {"42"}
    assert len(products) == 1
    assert products[0]["product_family"] == "loose_diamond"
    assert products[0]["attributes"] == []


def test_enrichment_mode_updates_saved_ids_and_skips_current_schema(tmp_path):
    database = tmp_path / "catalog.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT)")
    pending = {
        "_record_type": "product",
        "id": "42",
        "name": "Pending diamond",
        "url": "https://example.test/diamond/42/",
        "product_family": "loose_diamond",
        "type": "simple",
        "media": [],
        "variations": [],
    }
    current = {
        **pending,
        "id": "43",
        "url": "https://example.test/diamond/43/",
        "enrichment_schema_version": 1,
    }
    connection.executemany(
        "INSERT INTO products VALUES (?, ?)",
        [(item["id"], json.dumps(item)) for item in (pending, current)],
    )
    connection.commit()
    connection.close()

    spider = WooCommerceCatalogSpider(
        output_dir=str(tmp_path),
        resume_existing=True,
        enrichment_mode=True,
    )
    requests = list(spider.start_requests())

    assert spider.existing_product_ids == set()
    assert [request.url for request in requests] == [pending["url"]]
    assert spider.products_scheduled == 1
    assert spider.products_already_enriched == 1
    assert list(spider._emit_product(pending))[0]["id"] == "42"


def test_successful_and_missing_pages_are_versioned_for_resumable_enrichment():
    spider = WooCommerceCatalogSpider(enrichment_mode=True)
    base = {
        "_record_type": "product",
        "id": "42",
        "name": "Diamond",
        "type": "simple",
        "media": [],
        "variations": [],
    }

    completed = list(
        spider.parse_product_page(
            response_for("https://example.test/diamond/42/", "<main><h1>Diamond</h1></main>"),
            dict(base),
        )
    )[-1]
    unavailable = list(
        spider.parse_product_page(
            response_for("https://example.test/diamond/43/", "Not found", status=404),
            {**base, "id": "43"},
        )
    )[-1]

    assert completed["enrichment_schema_version"] == 1
    assert unavailable["enrichment_schema_version"] == 1


def test_product_limit_does_not_close_taxonomy_requests_early():
    spider = WooCommerceCatalogSpider(max_products=1)
    spider._crawler = type("Crawler", (), {"engine": type("Engine", (), {})()})()

    emitted = list(spider._emit_product({"id": "1", "_record_type": "product"}))

    assert len(emitted) == 1
    assert spider.products_emitted == 1


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
            "attachment_id": "901",
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


def test_parent_store_record_restores_missing_variation_attributes():
    product = {
        "name": "Alexandra Ring",
        "raw_api": {
            "variations": [
                {
                    "id": 501,
                    "attributes": [{"name": "Metal", "value": "14k-white-gold"}],
                }
            ]
        },
    }
    variations = [
        {
            "id": 501,
            "name": "Alexandra Ring",
            "attributes": {},
            "gallery": [{"source_url": "https://example.test/white.jpg"}],
        }
    ]

    WooCommerceCatalogSpider._merge_parent_variation_attributes(
        product, variations
    )

    assert variations[0]["attributes"] == {"Metal": "14k-white-gold"}
    assert variations[0]["name"] == "Alexandra Ring — Metal: 14k-white-gold"


def test_media_deduplication_prefers_semantic_roles_and_keeps_variation_links():
    spider = WooCommerceCatalogSpider()
    source_url = "https://example.test/ring.jpg"

    media = spider._dedupe_media(
        [
            {"role": "gallery", "position": 0, "source_url": source_url},
            {"role": "json_ld", "position": 0, "source_url": source_url},
            {"role": "featured", "position": 0, "source_url": source_url},
            {
                "role": "variation",
                "variation_id": 501,
                "source_url": source_url,
            },
            {
                "role": "variation",
                "variation_id": 502,
                "source_url": source_url,
            },
        ]
    )

    assert len(media) == 3
    assert media[0]["role"] == "featured"
    assert {item.get("variation_id") for item in media[1:]} == {501, 502}


def test_attachment_ids_are_resolved_into_each_variation_gallery():
    spider = WooCommerceCatalogSpider(base_url="https://example.test/")
    product = {
        "_record_type": "product",
        "id": "99",
        "name": "Adriana Ring",
        "media": [],
        "variations": [
            {
                "id": 501,
                "image_url": "https://example.test/front.jpg",
                "gallery": [],
                "gallery_image_ids": ["901", "902"],
            }
        ],
    }

    pending = list(spider._finish_product(product))
    assert len(pending) == 1
    media_request = pending[0]
    assert isinstance(media_request, Request)
    assert "wp-json/wp/v2/media" in media_request.url

    response = HtmlResponse(
        url=media_request.url,
        body=json.dumps(
            [
                {
                    "id": 901,
                    "source_url": "https://example.test/side.jpg",
                    "alt_text": "Side view",
                    "media_details": {"width": 1200, "height": 1200},
                },
                {
                    "id": 902,
                    "source_url": "https://example.test/hand.jpg",
                    "alt_text": "On hand",
                    "media_details": {"width": 1200, "height": 1200},
                },
            ]
        ).encode(),
        encoding="utf-8",
        request=media_request,
    )
    completed = list(spider.parse_variation_gallery_media(response))
    completed_product = next(item for item in completed if isinstance(item, dict))

    assert [
        item["source_url"] for item in completed_product["variations"][0]["gallery"]
    ] == [
        "https://example.test/side.jpg",
        "https://example.test/hand.jpg",
    ]
    assert sum(
        item["role"] == "variation_gallery" for item in completed_product["media"]
    ) == 2


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


def test_diamond_product_404_retries_with_public_diamond_sku_url():
    spider = WooCommerceCatalogSpider(enrich_html=True)
    product = {
        "_record_type": "product",
        "id": "2316967",
        "name": "Radiant Cut 3.07 Carat D Color VVS2 Clarity Lab Grown Diamond",
        "sku": "813614517",
        "url": "https://www.loosegrowndiamond.com/product/radiant-cut-3-07-carat-d-color-vvs2-clarity-lab-grown-diamond-813614517/",
        "product_family": "loose_diamond",
        "type": "simple",
    }

    # Simulate 404 on the WooCommerce /product/... URL
    first_req = Request(product["url"])
    first_resp = HtmlResponse(
        url=product["url"], status=404, body=b"Page not found", encoding="utf-8", request=first_req
    )

    retry_items = list(spider.parse_product_page(first_resp, api_product=product))
    assert len(retry_items) == 1
    retry_req = retry_items[0]
    assert isinstance(retry_req, Request)
    assert retry_req.url == "https://www.loosegrowndiamond.com/diamond/813614517/"

    # Simulate 200 on the /diamond/{sku}/ URL with diamond details grid
    grid_html = """
    <html><body>
    <div class="diamond-details-grid">
        <div class="dd-item">
            <div class="dd-label">Carat</div>
            <div class="dd-value"><span class="fw-bold">3.07</span></div>
        </div>
        <div class="dd-item">
            <div class="dd-label">Color</div>
            <div class="dd-value"><span class="fw-bold">D</span></div>
        </div>
        <div class="dd-item">
            <div class="dd-label">Clarity</div>
            <div class="dd-value"><span class="fw-bold">VVS2</span></div>
        </div>
    </div>
    </body></html>
    """.encode("utf-8")

    second_resp = HtmlResponse(
        url=retry_req.url, status=200, body=grid_html, encoding="utf-8", request=retry_req
    )
    final_items = list(spider.parse_product_page(second_resp, api_product=product))
    emitted = next(item for item in final_items if item.get("_record_type") == "product")
    assert emitted["diamond_details"]["carat"] == 3.07
    assert emitted["diamond_details"]["color"] == "D"
    assert emitted["diamond_details"]["clarity"] == "VVS2"
    assert emitted["html_enrichment"]["status"] == "complete"

