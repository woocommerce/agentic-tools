<?php
// Store settings for the disposable local site: an address, USD, guest checkout, offline
// payment methods that charge nothing, a shipping zone with a flat rate and local pickup,
// and the policy pages the shopping agent answers from. Runs under `wp eval-file`.
if ( ! defined( 'WP_CLI' ) ) {
	exit( 1 );
}
if ( ! class_exists( 'WooCommerce' ) ) {
	WP_CLI::error( 'WooCommerce is not active.' );
}

update_option( 'woocommerce_store_address', '12 Bench Lane' );
update_option( 'woocommerce_store_city', 'Toronto' );
update_option( 'woocommerce_default_country', 'CA:ON' );
update_option( 'woocommerce_store_postcode', 'M5V 1A1' );
update_option( 'woocommerce_currency', 'USD' );
update_option( 'woocommerce_allowed_countries', 'all' );
update_option( 'woocommerce_ship_to_countries', 'all' );
update_option( 'woocommerce_default_customer_address', 'base' );
update_option( 'woocommerce_calc_taxes', 'no' );
update_option( 'woocommerce_enable_guest_checkout', 'yes' );
update_option( 'woocommerce_enable_checkout_login_reminder', 'no' );
update_option( 'woocommerce_enable_reviews', 'yes' );
update_option( 'woocommerce_task_list_hidden', 'yes' );
update_option( 'woocommerce_onboarding_profile', array( 'skipped' => true, 'completed' => true ) );
update_option( 'woocommerce_coming_soon', 'no' );

foreach ( array( 'bacs' => 'Direct bank transfer', 'cheque' => 'Check payments', 'cod' => 'Cash on delivery' ) as $gateway => $title ) {
	$settings = get_option( "woocommerce_{$gateway}_settings", array() );
	$settings = is_array( $settings ) ? $settings : array();
	update_option( "woocommerce_{$gateway}_settings", array_merge( $settings, array(
		'enabled'     => 'yes',
		'title'       => $title,
		'description' => 'Local test store: no real payment is collected.',
	) ) );
}

// One shipping zone covering everywhere, with a flat rate and local pickup.
$zone_id = 0;
foreach ( WC_Shipping_Zones::get_zones() as $zone ) {
	if ( 'Everywhere' === $zone['zone_name'] ) {
		$zone_id = (int) $zone['zone_id'];
	}
}
$zone = $zone_id ? new WC_Shipping_Zone( $zone_id ) : new WC_Shipping_Zone();
$zone->set_zone_name( 'Everywhere' );
$zone->set_zone_order( 1 );
$zone->save();
$existing = array();
foreach ( $zone->get_shipping_methods( false ) as $method ) {
	$existing[ $method->id ] = (int) $method->instance_id;
}
$wanted = array(
	'flat_rate'    => array( 'title' => 'Standard shipping (3-5 days)', 'cost' => '6.00', 'tax_status' => 'none' ),
	'local_pickup' => array( 'title' => 'Pick up at the workshop', 'cost' => '0', 'tax_status' => 'none' ),
);
foreach ( $wanted as $method_id => $settings ) {
	$instance_id = $existing[ $method_id ] ?? $zone->add_shipping_method( $method_id );
	$method      = WC_Shipping_Zones::get_shipping_method( $instance_id );
	if ( $method && method_exists( $method, 'get_instance_option_key' ) ) {
		$current = is_array( $method->instance_settings ) ? $method->instance_settings : array();
		update_option( $method->get_instance_option_key(), array_merge( $current, array( 'enabled' => 'yes' ), $settings ) );
	}
}
WC_Cache_Helper::get_transient_version( 'shipping', true );

// The policy pages the shopping agent's search_policies reads (published pages).
$pages = array(
	'returns-and-refunds' => array(
		'title'   => 'Returns and refunds',
		'content' => '<p>Return anything unused within 30 days of delivery for a full refund to the original payment method. Refunds are issued within 5 business days of the return arriving at the workshop. Tools that have been used or sharpened can be exchanged but not refunded. Contact us for a return label; return shipping is free for faulty items and $6 otherwise.</p>',
	),
	'shipping-policy' => array(
		'title'   => 'Shipping',
		'content' => '<p>Orders ship from Toronto within 2 business days. Standard shipping is $6 flat and takes 3 to 5 business days across Canada and the US; orders over $75 ship free. Local pickup at the workshop is free and ready the next business day. We do not ship outside Canada and the US yet.</p>',
	),
	'warranty' => array(
		'title'   => 'Warranty',
		'content' => '<p>Every ACME tool carries a two-year warranty against defects in materials and workmanship. Wear from normal use, misuse, and modifications are not covered. Send a photo and your order number and we will repair or replace the item.</p>',
	),
);
foreach ( $pages as $slug => $page ) {
	$found = get_page_by_path( $slug );
	$data  = array(
		'post_name'    => $slug,
		'post_title'   => $page['title'],
		'post_content' => $page['content'],
		'post_status'  => 'publish',
		'post_type'    => 'page',
	);
	if ( $found ) {
		$data['ID'] = $found->ID;
	}
	wp_insert_post( $data );
}

WP_CLI::success( 'WooCommerce local settings configured.' );
