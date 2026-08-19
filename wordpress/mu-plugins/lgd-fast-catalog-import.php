<?php
/**
 * Plugin Name: LGD Fast Catalog Import
 * Description: Imports catalog images as remote S3 attachments and increases the WooCommerce CSV batch size.
 * Version: 1.0.0
 */

defined( 'ABSPATH' ) || exit;

const LGD_REMOTE_FEATURED_META = '_lgd_remote_featured_url';
const LGD_REMOTE_GALLERY_META  = '_lgd_remote_gallery_urls';
const LGD_REMOTE_URL_META      = '_lgd_remote_url';

/**
 * The local DDEV site has unlimited PHP execution time and memory. Larger
 * batches avoid thousands of browser round trips while keeping each request
 * bounded.
 */
add_filter(
	'woocommerce_product_import_batch_size',
	static function (): int {
		return 200;
	}
);

/**
 * Move external image URLs out of WooCommerce's synchronous sideload path.
 * The URLs stay on the product as import metadata until lightweight attachment
 * records are assigned immediately before the product is saved.
 *
 * @param array<string, mixed> $data Parsed WooCommerce importer data.
 * @return array<string, mixed>
 */
add_filter(
	'woocommerce_product_import_process_item_data',
	static function ( array $data ): array {
		$featured_url = '';
		$gallery_urls = array();

		if ( ! empty( $data['raw_image_id'] ) && wp_http_validate_url( $data['raw_image_id'] ) ) {
			$featured_url = esc_url_raw( $data['raw_image_id'] );
			unset( $data['raw_image_id'] );
		}

		if ( ! empty( $data['raw_gallery_image_ids'] ) && is_array( $data['raw_gallery_image_ids'] ) ) {
			$local_images = array();
			foreach ( $data['raw_gallery_image_ids'] as $image ) {
				if ( wp_http_validate_url( $image ) ) {
					$gallery_urls[] = esc_url_raw( $image );
				} else {
					$local_images[] = $image;
				}
			}

			if ( $local_images ) {
				$data['raw_gallery_image_ids'] = $local_images;
			} else {
				unset( $data['raw_gallery_image_ids'] );
			}
		}

		if ( $featured_url || $gallery_urls ) {
			if ( empty( $data['meta_data'] ) || ! is_array( $data['meta_data'] ) ) {
				$data['meta_data'] = array();
			}
			if ( $featured_url ) {
				$data['meta_data'][] = array(
					'key'   => LGD_REMOTE_FEATURED_META,
					'value' => $featured_url,
				);
			}
			if ( $gallery_urls ) {
				$data['meta_data'][] = array(
					'key'   => LGD_REMOTE_GALLERY_META,
					'value' => wp_json_encode( array_values( array_unique( $gallery_urls ) ) ),
				);
			}
		}

		return $data;
	}
);

/**
 * Return an existing remote attachment or create a database-only attachment.
 */
function lgd_remote_attachment_id( string $url ): int {
	$url = esc_url_raw( $url );
	if ( ! $url ) {
		return 0;
	}

	$slug     = 'lgd-remote-' . sha1( $url );
	$existing = get_page_by_path( $slug, OBJECT, 'attachment' );
	if ( $existing instanceof WP_Post ) {
		return (int) $existing->ID;
	}

	$path      = (string) wp_parse_url( $url, PHP_URL_PATH );
	$file_name = sanitize_file_name( wp_basename( $path ) );
	$file_type = wp_check_filetype( $file_name );
	$mime_type = $file_type['type'] ?: 'image/jpeg';
	$title     = sanitize_text_field( pathinfo( $file_name, PATHINFO_FILENAME ) );

	$attachment_id = wp_insert_attachment(
		array(
			'post_title'     => $title ?: 'Remote catalog image',
			'post_name'      => $slug,
			'post_status'    => 'inherit',
			'post_mime_type' => $mime_type,
			'guid'           => $url,
		),
		false,
		0,
		true
	);

	if ( is_wp_error( $attachment_id ) ) {
		return 0;
	}

	update_post_meta( $attachment_id, LGD_REMOTE_URL_META, $url );
	update_post_meta( $attachment_id, '_wp_attachment_image_alt', $title );

	return (int) $attachment_id;
}

/**
 * Return a stable identity for an attachment regardless of whether WordPress
 * downloaded it locally or the fast importer created an S3-backed record.
 *
 * WooCommerce can assign two different attachment IDs to the same source
 * image when a product is first imported normally and later updated through
 * the fast importer. Comparing IDs alone therefore does not prevent duplicate
 * gallery images.
 */
function lgd_attachment_image_identity( int $attachment_id ): string {
	if ( $attachment_id <= 0 ) {
		return '';
	}

	$source = (string) get_post_meta( $attachment_id, LGD_REMOTE_URL_META, true );
	if ( ! $source ) {
		$source = (string) get_post_meta( $attachment_id, '_wp_attached_file', true );
	}
	if ( ! $source ) {
		$source = (string) get_post_field( 'guid', $attachment_id );
	}

	$path      = (string) wp_parse_url( $source, PHP_URL_PATH );
	$file_name = rawurldecode( wp_basename( $path ?: $source ) );
	$file_name = (string) preg_replace(
		'/-[0-9a-f]{12}(?=\.[^.]+$)/i',
		'',
		$file_name
	);

	return strtolower( sanitize_file_name( $file_name ) );
}

/**
 * Merge gallery IDs while preferring the newly imported S3 records and
 * removing duplicate images represented by different attachment IDs.
 *
 * @param array<int, int|string> $preferred_ids Newly imported attachment IDs.
 * @param array<int, int|string> $existing_ids  Existing WooCommerce gallery IDs.
 * @return array<int, int>
 */
function lgd_dedupe_gallery_image_ids( array $preferred_ids, array $existing_ids, int $featured_id = 0 ): array {
	$gallery = array();
	$seen    = array();

	$featured_identity = lgd_attachment_image_identity( $featured_id );
	if ( $featured_identity ) {
		$seen[ $featured_identity ] = true;
	}

	foreach ( array_merge( $preferred_ids, $existing_ids ) as $attachment_id ) {
		$attachment_id = absint( $attachment_id );
		if ( ! $attachment_id ) {
			continue;
		}

		$identity = lgd_attachment_image_identity( $attachment_id );
		$key      = $identity ?: 'attachment:' . $attachment_id;
		if ( isset( $seen[ $key ] ) ) {
			continue;
		}

		$seen[ $key ] = true;
		$gallery[]    = $attachment_id;
	}

	return $gallery;
}

/**
 * Assign remote attachment IDs without downloading image bytes.
 *
 * @param WC_Product           $product Product being imported.
 * @param array<string, mixed> $data    Parsed WooCommerce importer data.
 * @return WC_Product
 */
add_filter(
	'woocommerce_product_import_pre_insert_product_object',
	static function ( $product, array $data ) {
		$featured_url = '';
		$gallery_urls = array();

		foreach ( $data['meta_data'] ?? array() as $meta ) {
			if ( LGD_REMOTE_FEATURED_META === ( $meta['key'] ?? '' ) ) {
				$featured_url = (string) ( $meta['value'] ?? '' );
			}
			if ( LGD_REMOTE_GALLERY_META === ( $meta['key'] ?? '' ) ) {
				$decoded = json_decode( (string) ( $meta['value'] ?? '' ), true );
				if ( is_array( $decoded ) ) {
					$gallery_urls = $decoded;
				}
			}
		}

		if ( $featured_url ) {
			$featured_id = lgd_remote_attachment_id( $featured_url );
			if ( $featured_id ) {
				$product->set_image_id( $featured_id );
			}
		}

		if ( $gallery_urls ) {
			$gallery_ids = array_filter( array_map( 'lgd_remote_attachment_id', $gallery_urls ) );
			$product->set_gallery_image_ids(
				lgd_dedupe_gallery_image_ids(
					array_values( $gallery_ids ),
					$product->get_gallery_image_ids(),
					(int) $product->get_image_id()
				)
			);
		}

		return $product;
	},
	10,
	2
);

/**
 * Remote attachments intentionally have no local _wp_attached_file value.
 */
add_filter(
	'wp_get_attachment_url',
	static function ( $url, int $attachment_id ) {
		$remote_url = get_post_meta( $attachment_id, LGD_REMOTE_URL_META, true );
		return $remote_url ? esc_url_raw( $remote_url ) : $url;
	},
	10,
	2
);

/**
 * WordPress normally refuses to render an attachment image when there is no
 * local _wp_attached_file. Remote catalog attachments intentionally have no
 * local file, so provide the requested display dimensions and S3 URL directly.
 */
add_filter(
	'image_downsize',
	static function ( $downsize, int $attachment_id, $size ) {
		$remote_url = get_post_meta( $attachment_id, LGD_REMOTE_URL_META, true );
		if ( ! $remote_url ) {
			return $downsize;
		}

		$width  = 1200;
		$height = 1200;
		if ( is_array( $size ) ) {
			$width  = max( 1, absint( $size[0] ?? $width ) );
			$height = max( 1, absint( $size[1] ?? $height ) );
		} elseif ( is_string( $size ) && 'full' !== $size ) {
			$subsizes = wp_get_registered_image_subsizes();
			if ( isset( $subsizes[ $size ] ) ) {
				$width  = max( 1, absint( $subsizes[ $size ]['width'] ?? $width ) );
				$height = max( 1, absint( $subsizes[ $size ]['height'] ?? $height ) );
			}
		}

		return array( esc_url_raw( $remote_url ), $width, $height, false );
	},
	10,
	3
);
