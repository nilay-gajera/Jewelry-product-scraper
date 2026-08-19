<?php
/**
 * Align existing WooCommerce parent galleries to a validated LGD master CSV.
 *
 * Usage inside the DDEV project:
 *   ddev wp eval-file wp-content/tools/lgd-align-product-images.php \
 *     /var/www/html/imports/woocommerce-master-source-path-dedup.csv
 *
 * Set LGD_APPLY=1 to persist changes and delete attachment records that become
 * unreferenced. Remote S3 objects are never deleted by this script.
 */

defined( 'ABSPATH' ) || exit;

if ( ! function_exists( 'lgd_remote_attachment_id' ) ) {
	WP_CLI::error( 'LGD Fast Catalog Import must-use plugin is not active.' );
}

$csv_path = isset( $args[0] ) ? (string) $args[0] : '';
$apply    = ( isset( $args[1] ) && 'apply' === (string) $args[1] )
	|| '1' === (string) getenv( 'LGD_APPLY' );

if ( ! $csv_path || ! is_readable( $csv_path ) ) {
	WP_CLI::error( 'Pass a readable WooCommerce master CSV path.' );
}

/** Return an existing remote attachment ID without creating a record. */
function lgd_existing_remote_attachment_id( string $url ): int {
	$slug     = 'lgd-remote-' . sha1( esc_url_raw( $url ) );
	$existing = get_page_by_path( $slug, OBJECT, 'attachment' );
	return $existing instanceof WP_Post ? (int) $existing->ID : 0;
}

/** @return array<int, int> */
function lgd_current_product_image_ids( int $product_id ): array {
	$ids      = array();
	$featured = absint( get_post_meta( $product_id, '_thumbnail_id', true ) );
	if ( $featured ) {
		$ids[] = $featured;
	}
	$gallery = (string) get_post_meta( $product_id, '_product_image_gallery', true );
	foreach ( array_filter( array_map( 'absint', explode( ',', $gallery ) ) ) as $id ) {
		$ids[] = $id;
	}
	return array_values( array_unique( $ids ) );
}

/** @return array<int, int> */
function lgd_all_active_attachment_ids(): array {
	global $wpdb;

	$active = array();
	foreach ( $wpdb->get_col( "SELECT meta_value FROM {$wpdb->postmeta} WHERE meta_key = '_thumbnail_id'" ) as $value ) {
		$id = absint( $value );
		if ( $id ) {
			$active[ $id ] = $id;
		}
	}
	foreach ( $wpdb->get_col( "SELECT meta_value FROM {$wpdb->postmeta} WHERE meta_key = '_product_image_gallery'" ) as $value ) {
		foreach ( array_filter( array_map( 'absint', explode( ',', (string) $value ) ) ) as $id ) {
			$active[ $id ] = $id;
		}
	}
	return array_values( $active );
}

global $wpdb;

$sku_to_product = array();
$rows           = $wpdb->get_results(
	"SELECT p.ID, pm.meta_value AS sku
	 FROM {$wpdb->posts} p
	 INNER JOIN {$wpdb->postmeta} pm ON pm.post_id = p.ID AND pm.meta_key = '_sku'
	 WHERE p.post_type = 'product' AND p.post_status != 'trash' AND pm.meta_value != ''"
);
foreach ( $rows as $row ) {
	$sku_to_product[ (string) $row->sku ] = (int) $row->ID;
}

$file   = new SplFileObject( $csv_path, 'r' );
$header = $file->fgetcsv();
if ( ! is_array( $header ) ) {
	WP_CLI::error( 'CSV header is missing.' );
}
$header[0] = preg_replace( '/^\xEF\xBB\xBF/', '', (string) $header[0] );
$columns   = array_flip( $header );
foreach ( array( 'Type', 'SKU', 'Images' ) as $required ) {
	if ( ! isset( $columns[ $required ] ) ) {
		WP_CLI::error( "CSV column {$required} is missing." );
	}
}

$stats = array(
	'apply'                         => $apply,
	'imported_parent_rows'          => 0,
	'changed_products'              => 0,
	'removed_gallery_references'    => 0,
	'missing_attachment_urls'       => 0,
	'deleted_attachment_records'    => 0,
	'failed_attachment_deletions'   => array(),
);
$candidate_attachment_ids = array();

while ( ! $file->eof() ) {
	$row = $file->fgetcsv();
	if ( ! is_array( $row ) || array( null ) === $row ) {
		continue;
	}
	$type = trim( (string) ( $row[ $columns['Type'] ] ?? '' ) );
	if ( 'variation' === $type ) {
		continue;
	}
	$sku = trim( (string) ( $row[ $columns['SKU'] ] ?? '' ) );
	if ( ! $sku || ! isset( $sku_to_product[ $sku ] ) ) {
		continue;
	}

	$images = array_values(
		array_unique(
			array_filter(
				array_map( 'trim', explode( ',', (string) ( $row[ $columns['Images'] ] ?? '' ) ) )
			)
		)
	);
	if ( ! $images ) {
		continue;
	}

	++$stats['imported_parent_rows'];
	$product_id = $sku_to_product[ $sku ];
	$desired_ids = array();
	foreach ( $images as $url ) {
		$attachment_id = lgd_existing_remote_attachment_id( $url );
		if ( ! $attachment_id && $apply ) {
			$attachment_id = lgd_remote_attachment_id( $url );
		}
		if ( ! $attachment_id ) {
			++$stats['missing_attachment_urls'];
			continue 2;
		}
		$desired_ids[] = $attachment_id;
	}

	$current_ids = lgd_current_product_image_ids( $product_id );
	if ( $current_ids === $desired_ids ) {
		continue;
	}

	++$stats['changed_products'];
	$stats['removed_gallery_references'] += max( 0, count( $current_ids ) - count( $desired_ids ) );
	foreach ( array_diff( $current_ids, $desired_ids ) as $attachment_id ) {
		$candidate_attachment_ids[ $attachment_id ] = $attachment_id;
	}

	if ( ! $apply ) {
		continue;
	}

	update_post_meta( $product_id, '_thumbnail_id', $desired_ids[0] );
	update_post_meta( $product_id, '_product_image_gallery', implode( ',', array_slice( $desired_ids, 1 ) ) );
	update_post_meta( $product_id, LGD_REMOTE_FEATURED_META, $images[0] );
	update_post_meta(
		$product_id,
		LGD_REMOTE_GALLERY_META,
		wp_json_encode( array_slice( $images, 1 ) )
	);
	clean_post_cache( $product_id );
	wc_delete_product_transients( $product_id );
}

if ( $apply && $candidate_attachment_ids ) {
	$active_ids = array_flip( lgd_all_active_attachment_ids() );
	foreach ( $candidate_attachment_ids as $attachment_id ) {
		if ( isset( $active_ids[ $attachment_id ] ) ) {
			continue;
		}
		if ( 'attachment' !== get_post_type( $attachment_id ) ) {
			continue;
		}
		if ( wp_delete_attachment( $attachment_id, true ) ) {
			++$stats['deleted_attachment_records'];
		} else {
			$stats['failed_attachment_deletions'][] = $attachment_id;
		}
	}
}

WP_CLI::line( wp_json_encode( $stats, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES ) );
