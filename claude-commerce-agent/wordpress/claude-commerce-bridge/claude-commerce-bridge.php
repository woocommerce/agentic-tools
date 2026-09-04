<?php
/**
 * Plugin Name: Claude Commerce Bridge
 * Plugin URI:  https://github.com/Automattic/claude-commerce-agent
 * Description: Hands a shopping agent's Store API cart to the shopper's browser for checkout on the store's own pages, and lets the agent read back the orders placed from that cart. Nothing here places an order or takes payment.
 * Version:     0.1.0
 * Author:      Automattic
 * License:     Apache-2.0
 * Requires Plugins: woocommerce
 * Requires at least: 6.5
 * Requires PHP: 8.0
 *
 * Copyright 2026 Automattic Inc.
 * SPDX-License-Identifier: Apache-2.0
 *
 * The agent builds a cart through the public Store API under a Cart-Token. The shopper's
 * browser holds a different WooCommerce session, so the cart has to change hands before
 * the store's checkout page can see it. Three small pieces do that:
 *
 *  1. GET /?claude_commerce_cart=<token>   The handoff. The token is validated the way the
 *     Store API validates it, the source session's cart lines are re-added to the browser's
 *     cart (through WC_Cart::add_to_cart, so stock and purchasability are checked again), the
 *     source session's id is remembered on the browser's session, and the browser is sent to
 *     the checkout page. An expired or unknown token lands on the cart page with a notice.
 *  2. On checkout (classic and block), the order records which agent cart it came from, and
 *     once that order reaches a status the store acts on, the source session is discarded.
 *  3. GET /wp-json/claude-commerce/v1/orders with a Cart-Token header answers the orders
 *     placed from that cart: the token that built the cart is the credential that reads them.
 *
 * /wp-json/claude-commerce/v1/ (status) and /brand (name, tagline, logo, theme colors,
 * checkout URL) round it out. Everything here is public and keyed by the token. The only
 * things the store writes are the shopper's own cart, the stamp on their order, and the
 * spent agent session.
 *
 * This is demonstration code. See wordpress/README.md for what it does not do that a store
 * open to the public would need.
 */

namespace ClaudeCommerce\Bridge;

defined( 'ABSPATH' ) || exit;

const VERSION      = '0.1.0';
const REST_NS      = 'claude-commerce/v1';
const QUERY_VAR    = 'claude_commerce_cart';
const SESSION_KEY  = 'claude_commerce_source';
const ORDER_META   = '_claude_commerce_cart';
const ORDER_LIMIT  = 20;
const HEX_COLOR    = '/^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i';

// -- Feature compatibility ------------------------------------------------------------

add_action( 'before_woocommerce_init', __NAMESPACE__ . '\\declare_feature_compatibility' );

/** Orders are read through wc_get_orders and stamped through the CRUD, so neither storage nor checkout needs the legacy path. */
function declare_feature_compatibility(): void {
	$features = '\Automattic\WooCommerce\Utilities\FeaturesUtil';
	if ( ! class_exists( $features ) ) {
		return;
	}
	$features::declare_compatibility( 'custom_order_tables', __FILE__, true );
	$features::declare_compatibility( 'cart_checkout_blocks', __FILE__, true );
}

/**
 * The payload of a Store API cart token, or null when the token is not one this store
 * issued (or has expired). Uses WooCommerce's own utilities so the rule is the store's.
 *
 * @return array<string,mixed>|null
 */
function token_payload( string $token ): ?array {
	$token = trim( $token );
	if ( '' === $token ) {
		return null;
	}
	if ( class_exists( '\Automattic\WooCommerce\StoreApi\Utilities\CartTokenUtils' ) ) {
		$utils = '\Automattic\WooCommerce\StoreApi\Utilities\CartTokenUtils';
		if ( ! $utils::validate_cart_token( $token ) ) {
			return null;
		}
		$payload = $utils::get_cart_token_payload( $token );
		return is_array( $payload ) ? $payload : (array) $payload;
	}
	if ( class_exists( '\Automattic\WooCommerce\StoreApi\Utilities\JsonWebToken' ) ) {
		$jwt = '\Automattic\WooCommerce\StoreApi\Utilities\JsonWebToken';
		if ( ! $jwt::validate( $token, '@' . wp_salt() ) ) {
			return null;
		}
		$parts = $jwt::get_parts( $token );
		return isset( $parts->payload ) ? (array) $parts->payload : null;
	}
	return null;
}

/** The token a request presents, from the Cart-Token header. */
function request_token( \WP_REST_Request $request ): string {
	return (string) $request->get_header( 'cart-token' );
}

// -- REST: status, brand, orders -----------------------------------------------------

add_action(
	'rest_api_init',
	function () {
		register_rest_route(
			REST_NS,
			'/',
			array(
				'methods'             => 'GET',
				'permission_callback' => '__return_true',
				'callback'            => function () {
					return array(
						'plugin'            => 'claude-commerce-bridge',
						'version'           => VERSION,
						'handoff'           => true,
						'handoff_query_var' => QUERY_VAR,
						'orders'            => true,
					);
				},
			)
		);
		register_rest_route(
			REST_NS,
			'/brand',
			array(
				'methods'             => 'GET',
				'permission_callback' => '__return_true',
				'callback'            => __NAMESPACE__ . '\\brand',
			)
		);
		register_rest_route(
			REST_NS,
			'/orders',
			array(
				'methods'             => 'GET',
				'permission_callback' => '__return_true',
				'callback'            => __NAMESPACE__ . '\\orders',
			)
		);
	}
);

/** Name, tagline, logo, the theme's global colors (when they are literal hex values), and the store's own checkout and cart URLs. */
function brand(): array {
	$logo_id  = (int) get_theme_mod( 'custom_logo' );
	$logo_url = $logo_id ? wp_get_attachment_image_url( $logo_id, 'full' ) : null;
	$colors   = null;
	if ( function_exists( 'wp_get_global_styles' ) ) {
		$styles     = wp_get_global_styles( array( 'color' ) );
		$background = is_array( $styles ) ? ( $styles['background'] ?? null ) : null;
		$foreground = is_array( $styles ) ? ( $styles['text'] ?? null ) : null;
		if ( is_string( $background ) && is_string( $foreground )
			&& preg_match( HEX_COLOR, $background )
			&& preg_match( HEX_COLOR, $foreground ) ) {
			$colors = array(
				'background' => $background,
				'foreground' => $foreground,
			);
		}
	}
	return array(
		'name'         => get_bloginfo( 'name' ),
		'tagline'      => get_bloginfo( 'description' ) ?: null,
		'logo_url'     => $logo_url ?: null,
		'colors'       => $colors,
		'currency'     => function_exists( 'get_woocommerce_currency' ) ? get_woocommerce_currency() : null,
		'checkout_url' => function_exists( 'wc_get_checkout_url' ) ? wc_get_checkout_url() : null,
		'cart_url'     => function_exists( 'wc_get_cart_url' ) ? wc_get_cart_url() : null,
	);
}

/**
 * The orders placed from the cart the presented token names, newest first. Only orders
 * this plugin stamped at checkout are visible; a token that validates but built no order
 * gets an empty list, and a token that does not validate gets a 403.
 */
function orders( \WP_REST_Request $request ) {
	$payload = token_payload( request_token( $request ) );
	if ( null === $payload || empty( $payload['user_id'] ) ) {
		return new \WP_Error(
			'claude_commerce_invalid_cart_token',
			__( 'The Cart-Token header is missing, invalid, or expired.', 'claude-commerce-bridge' ),
			array( 'status' => 403 )
		);
	}
	$source = (string) $payload['user_id'];
	$orders = wc_get_orders(
		array(
			'limit'      => ORDER_LIMIT,
			'orderby'    => 'date',
			'order'      => 'DESC',
			'meta_key'   => ORDER_META, // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_key
			'meta_value' => $source, // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_value
		)
	);
	$rows = array();
	foreach ( $orders as $order ) {
		// The query narrows; this is the check that holds whatever the order store does
		// with a meta filter: an order that does not carry this cart's stamp is not shown.
		if ( ! $order instanceof \WC_Order || (string) $order->get_meta( ORDER_META ) !== $source ) {
			continue;
		}
		$lines = array();
		foreach ( $order->get_items( 'line_item' ) as $item ) {
			/** @var \WC_Order_Item_Product $item */
			$lines[] = array(
				'product_id'   => $item->get_product_id(),
				'variation_id' => $item->get_variation_id(),
				'name'         => $item->get_name(),
				'quantity'     => $item->get_quantity(),
				'total'        => $item->get_total(),
			);
		}
		$created = $order->get_date_created();
		$rows[]  = array(
			'id'               => $order->get_id(),
			'number'           => $order->get_order_number(),
			'status'           => $order->get_status(),
			'currency'         => $order->get_currency(),
			'total'            => $order->get_total(),
			'date_created_gmt' => $created ? gmdate( 'Y-m-d\TH:i:s', $created->getTimestamp() ) : null,
			'line_items'       => $lines,
			/**
			 * A tracking URL for the order, when an extension knows one. Shipment tracking
			 * extensions can hook this; core records none.
			 *
			 * @param string|null $url   The tracking URL, or null.
			 * @param \WC_Order   $order The order.
			 */
			'tracking_url'     => apply_filters( 'claude_commerce_order_tracking_url', null, $order ),
		);
	}
	return rest_ensure_response( array( 'orders' => $rows ) );
}

// -- The handoff: adopt the agent's cart into the browser's session -----------------------

add_action( 'template_redirect', __NAMESPACE__ . '\\handoff', 5 );

function handoff(): void {
	if ( ! isset( $_GET[ QUERY_VAR ] ) || ! is_string( $_GET[ QUERY_VAR ] ) || ! function_exists( 'WC' ) ) { // phpcs:ignore WordPress.Security.NonceVerification.Recommended
		return;
	}
	$token   = sanitize_text_field( wp_unslash( $_GET[ QUERY_VAR ] ) ); // phpcs:ignore WordPress.Security.NonceVerification.Recommended
	$payload = token_payload( $token );
	if ( ( null === WC()->session || null === WC()->cart ) && function_exists( 'wc_load_cart' ) ) {
		wc_load_cart();
	}
	if ( null === WC()->session || null === WC()->cart ) {
		return;
	}
	if ( null === $payload || empty( $payload['user_id'] ) ) {
		wc_add_notice( __( 'That cart link has expired. Please add the items again.', 'claude-commerce-bridge' ), 'error' );
		wp_safe_redirect( wc_get_cart_url() );
		exit;
	}
	$source_id = (string) $payload['user_id'];
	$session   = WC()->session;
	$data      = method_exists( $session, 'get_session' ) ? $session->get_session( $source_id ) : false;
	$lines     = is_array( $data ) && isset( $data['cart'] ) ? maybe_unserialize( $data['cart'] ) : array();
	$coupons   = is_array( $data ) && isset( $data['applied_coupons'] ) ? maybe_unserialize( $data['applied_coupons'] ) : array();
	if ( ! is_array( $lines ) || empty( $lines ) ) {
		wc_add_notice( __( 'That cart is empty or has expired. Please add the items again.', 'claude-commerce-bridge' ), 'notice' );
		wp_safe_redirect( wc_get_cart_url() );
		exit;
	}

	// The browser's cart becomes the agent's: emptied, then rebuilt line by line through
	// the same path the storefront's own buttons use, so stock and purchasability are
	// checked again now rather than trusted from the source session. The persistent cart is
	// left alone, so a signed-in shopper who abandons this checkout keeps the cart they had.
	WC()->cart->empty_cart( false );
	$missed = 0;
	foreach ( $lines as $line ) {
		if ( ! is_array( $line ) || empty( $line['product_id'] ) ) {
			continue;
		}
		$added = WC()->cart->add_to_cart(
			(int) $line['product_id'],
			max( 1, (int) ( $line['quantity'] ?? 1 ) ),
			(int) ( $line['variation_id'] ?? 0 ),
			is_array( $line['variation'] ?? null ) ? $line['variation'] : array()
		);
		if ( ! $added ) {
			++$missed;
		}
	}
	foreach ( (array) $coupons as $code ) {
		if ( is_string( $code ) && '' !== $code ) {
			WC()->cart->apply_coupon( $code );
		}
	}
	$session->set( SESSION_KEY, $source_id );
	$session->set_customer_session_cookie( true );
	if ( $missed > 0 ) {
		wc_add_notice(
			sprintf(
				/* translators: %d: number of items that could not be added. */
				_n( '%d item from the assistant\'s cart is no longer available and was left out.', '%d items from the assistant\'s cart are no longer available and were left out.', $missed, 'claude-commerce-bridge' ),
				$missed
			),
			'notice'
		);
	}
	wp_safe_redirect( WC()->cart->is_empty() ? wc_get_cart_url() : wc_get_checkout_url() );
	exit;
}

// -- Stamp the order with the cart it came from (classic and block checkout) ---------------

function stamp_order( $order ): void {
	if ( ! $order instanceof \WC_Order || null === WC()->session ) {
		return;
	}
	$source = WC()->session->get( SESSION_KEY );
	if ( is_string( $source ) && '' !== $source ) {
		$order->update_meta_data( ORDER_META, $source );
	}
}

add_action( 'woocommerce_checkout_create_order', __NAMESPACE__ . '\\stamp_order', 10, 1 );
add_action( 'woocommerce_store_api_checkout_update_order_meta', __NAMESPACE__ . '\\stamp_order', 10, 1 );

// -- Once the order is paid for, the agent's cart is spent ---------------------------------

add_action( 'woocommerce_order_status_changed', __NAMESPACE__ . '\\spend_source_cart', 20, 4 );

/**
 * Discard the source session once its cart has become an order the store is acting on, so the
 * agent's next cart read shows nothing left to buy. The orders stay readable: they are found by
 * the stamp on the order rather than by the session row, and a cart token whose session is gone
 * loads an empty cart rather than failing.
 *
 * A status change is what marks the moment. Both order_processed hooks run before payment is
 * taken, so they would spend the cart on a card that then declines.
 */
function spend_source_cart( $order_id, $status_from, $status_to, $order ): void {
	if ( ! in_array( $status_to, array( 'on-hold', 'processing', 'completed' ), true ) ) {
		return;
	}
	$source  = $order instanceof \WC_Order ? (string) $order->get_meta( ORDER_META ) : '';
	$session = function_exists( 'WC' ) ? WC()->session : null;
	if ( '' === $source || null === $session || ! method_exists( $session, 'delete_session' ) ) {
		return;
	}
	$session->delete_session( $source );
	if ( $source === $session->get( SESSION_KEY ) ) {
		$session->set( SESSION_KEY, null );
	}
}
