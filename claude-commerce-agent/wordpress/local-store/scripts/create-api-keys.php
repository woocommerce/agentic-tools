<?php
// A disposable read/write REST API key for the local admin, printed as JSON. Runs under
// `wp eval-file`; WooCommerce stores only a hash, so the key is regenerated each run.
if ( ! defined( 'WP_CLI' ) ) {
	exit( 1 );
}
if ( ! function_exists( 'wc_rand_hash' ) || ! function_exists( 'wc_api_hash' ) ) {
	WP_CLI::error( 'WooCommerce API key helpers are not available.' );
}
global $wpdb;
$login = getenv( 'WORDPRESS_ADMIN_USER' ) ?: 'store_admin';
$user  = get_user_by( 'login', $login );
if ( ! $user ) {
	$admins = get_users( array( 'role' => 'administrator', 'number' => 1 ) );
	$user   = $admins ? $admins[0] : null;
}
if ( ! $user ) {
	WP_CLI::error( 'No administrator user found for API key creation.' );
}
$description = 'claude-commerce-agent local key';
$table       = $wpdb->prefix . 'woocommerce_api_keys';
$wpdb->delete( $table, array( 'user_id' => (int) $user->ID, 'description' => $description ), array( '%d', '%s' ) );
$consumer_key    = 'ck_' . wc_rand_hash();
$consumer_secret = 'cs_' . wc_rand_hash();
$inserted        = $wpdb->insert(
	$table,
	array(
		'user_id'         => (int) $user->ID,
		'description'     => $description,
		'permissions'     => 'read_write',
		'consumer_key'    => wc_api_hash( $consumer_key ),
		'consumer_secret' => $consumer_secret,
		'truncated_key'   => substr( $consumer_key, -7 ),
	),
	array( '%d', '%s', '%s', '%s', '%s', '%s' )
);
if ( ! $inserted ) {
	WP_CLI::error( 'Unable to insert WooCommerce API key: ' . $wpdb->last_error );
}
echo wp_json_encode( array( 'consumer_key' => $consumer_key, 'consumer_secret' => $consumer_secret ) ) . PHP_EOL;
