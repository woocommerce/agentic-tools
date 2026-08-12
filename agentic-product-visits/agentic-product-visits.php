<?php
/**
 * Agentic product visits — single-file code snippet.
 *
 * Usage: paste into the Code Snippets plugin (run everywhere), or load it in your theme.
 *
 * Visits are split by brand only. Each brand counter lives in its own
 * flat meta key on the product so it can be incremented atomically with
 * a direct `meta_value + 1` UPDATE (a serialized array would force a
 * lossy read-modify-write):
 *
 *   _wcus_agentic_visits_brand_openai    => 7
 *   _wcus_agentic_visits_brand_anthropic => 5
 *   _wcus_agentic_visits_brand_other     => 2
 *
 * Brand values are canonical tokens from the detection tables below;
 * `other` is the catch-all for minor agents and for visitors that are
 * confidently agents but of undeterminable brand, so every detected
 * visit lands in some brand bucket. No separate total is stored — it
 * is the sum of the brand counts.
 *
 * Reading the data — two filters, both returning array<string,int> of
 * brand => visits (use array_sum() for the total):
 *
 *   // One product.
 *   $stats = apply_filters( 'wcus_agentic_product_visits', array(), $product_id );
 *
 *   // Sitewide aggregate across all products.
 *   $stats = apply_filters( 'wcus_agentic_visits_totals', array() );
 */

defined( 'ABSPATH' ) || exit;

if ( ! class_exists( 'WCUS_Agentic_Product_Visits' ) ) {

	/**
	 * Detects AI-agent visits to product pages and aggregates them in post meta.
	 *
	 * Detection signals are evaluated in decreasing order of reliability,
	 * first conclusive hit wins:
	 *
	 * 1. Web Bot Auth headers (self-identification designed for agents).
	 * 2. User-Agent signatures (known AI agent and crawler tokens).
	 * 3. AI referral hints — utm_source / Referer (typically a human visitor
	 *    arriving from an AI product).
	 */
	final class WCUS_Agentic_Product_Visits {

		const META_KEY = '_wcus_agentic_visits';

		/**
		 * Defer booting until all plugins are loaded, so the WooCommerce
		 * check works regardless of plugin load order.
		 *
		 * Snippet runners differ: Code Snippets executes before other
		 * plugins finish loading, a theme's functions.php after
		 * `plugins_loaded` has already fired — hence the did_action branch.
		 *
		 * @return void
		 */
		public static function register() {
			if ( did_action( 'plugins_loaded' ) ) {
				self::maybe_boot();
			} else {
				add_action( 'plugins_loaded', array( __CLASS__, 'maybe_boot' ) );
			}
		}

		/**
		 * Hook into the request lifecycle, only when WooCommerce is active.
		 *
		 * Without WooCommerce the snippet stays fully inert: no tracking
		 * hook, and the read filters return their caller-supplied default.
		 *
		 * Priority 0 mirrors the plugin: run before core handlers that may
		 * exit the request.
		 *
		 * @return void
		 */
		public static function maybe_boot() {
			if ( ! class_exists( 'WooCommerce' ) ) {
				return;
			}

			add_action( 'template_redirect', array( __CLASS__, 'track' ), 0 );

			add_filter( 'wcus_agentic_product_visits', array( __CLASS__, 'filter_product_stats' ), 10, 2 );
			add_filter( 'wcus_agentic_visits_totals', array( __CLASS__, 'filter_totals' ) );
		}

		/**
		 * Filter callback: per-brand visit counts for one product.
		 *
		 * Usage: `apply_filters( 'wcus_agentic_product_visits', array(), $product_id )`.
		 *
		 * @param mixed $stats      Ignored; replaced with the product's stats.
		 * @param int   $product_id Product post ID.
		 * @return array<string,int> Brand => visits.
		 */
		public static function filter_product_stats( $stats, $product_id ) {
			$prefix = self::META_KEY . '_brand_';
			$stats  = array();

			// All-meta read goes through the meta cache; one query per
			// product at most, none when the post is already warm.
			foreach ( (array) get_post_meta( (int) $product_id ) as $key => $values ) {
				if ( 0 === strpos( $key, $prefix ) && isset( $values[0] ) ) {
					$stats[ substr( $key, strlen( $prefix ) ) ] = (int) $values[0];
				}
			}

			return $stats;
		}

		/**
		 * Filter callback: sitewide per-brand visit counts across all products.
		 *
		 * Usage: `apply_filters( 'wcus_agentic_visits_totals', array() )`.
		 *
		 * @param mixed $stats Ignored; replaced with the sitewide stats.
		 * @return array<string,int> Brand => visits.
		 */
		public static function filter_totals( $stats ) {
			global $wpdb;

			$prefix = self::META_KEY . '_brand_';

			// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Cross-post SUM has no meta API equivalent; small bounded key space.
			$rows = $wpdb->get_results(
				$wpdb->prepare(
					"SELECT meta_key, SUM(CAST(meta_value AS UNSIGNED)) AS hits FROM {$wpdb->postmeta} WHERE meta_key LIKE %s GROUP BY meta_key",
					$wpdb->esc_like( $prefix ) . '%'
				),
				ARRAY_A
			);

			$stats = array();

			foreach ( (array) $rows as $row ) {
				$stats[ substr( $row['meta_key'], strlen( $prefix ) ) ] = (int) $row['hits'];
			}

			return $stats;
		}

		/**
		 * Record the visit when this is a trackable product page load and a
		 * detector signal hits.
		 *
		 * @return void
		 */
		public static function track() {
			if ( ! is_product() || ! self::should_track() ) {
				return;
			}

			$product_id = get_queried_object_id();

			if ( ! $product_id ) {
				return;
			}

			$brand = self::detect();

			if ( null === $brand ) {
				return;
			}

			self::record( $product_id, $brand );
		}

		/**
		 * Whether the current request should be considered at all.
		 *
		 * Site staff (edit_posts) are skipped so admins testing with spoofed
		 * user agents don't pollute the counts; anonymous visitors and
		 * customer-authenticated agent traffic remain tracked.
		 *
		 * @return bool
		 */
		private static function should_track() {
			if ( ( defined( 'DOING_CRON' ) && DOING_CRON )
				|| ( defined( 'WP_CLI' ) && WP_CLI )
				|| ( defined( 'XMLRPC_REQUEST' ) && XMLRPC_REQUEST ) ) {
				return false;
			}

			if ( is_user_logged_in() && current_user_can( 'edit_posts' ) ) {
				return false;
			}

			return true;
		}

		/**
		 * Increment the product's brand counter.
		 *
		 * The UPDATE is a relative `meta_value + 1`, so concurrent hits
		 * cannot overwrite each other. Only the very first hit falls back
		 * to `add_post_meta` (with the unique flag) to seed the row; the
		 * theoretical double-seed race on that first hit is the only way a
		 * count can be lost, ever.
		 *
		 * @param int    $product_id Product post ID.
		 * @param string $brand      Canonical brand value.
		 * @return void
		 */
		private static function record( $product_id, $brand ) {
			global $wpdb;

			$meta_key = self::META_KEY . '_brand_' . $brand;

			// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Atomic counter increment; the meta API can only read-modify-write.
			$updated = $wpdb->query(
				$wpdb->prepare(
					"UPDATE {$wpdb->postmeta} SET meta_value = CAST(meta_value AS UNSIGNED) + 1 WHERE post_id = %d AND meta_key = %s",
					$product_id,
					$meta_key
				)
			);

			if ( ! $updated ) {
				add_post_meta( $product_id, $meta_key, 1, true );
			}

			wp_cache_delete( $product_id, 'post_meta' );
		}

		/* ---------------------------------------------------------------
		 * Detection (condensed from the plugin's Detection\* classes).
		 * ------------------------------------------------------------- */

		/**
		 * Classify the current request.
		 *
		 * @return string|null Canonical brand, or null when no signal identifies an AI agent.
		 */
		private static function detect() {
			$brand = self::match_web_bot_auth();

			if ( null === $brand ) {
				$brand = self::match_user_agent();
			}

			if ( null === $brand ) {
				$brand = self::match_referral();
			}

			return $brand;
		}

		/**
		 * Web Bot Auth (IETF HTTP Message Signatures for bots) detection.
		 *
		 * Presence-only: the signature is NOT cryptographically verified, so
		 * these headers are trusted the same as a self-declared User-Agent.
		 *
		 * @return string|null Canonical brand, or null when the headers are absent.
		 */
		private static function match_web_bot_auth() {
			$agent = isset( $_SERVER['HTTP_SIGNATURE_AGENT'] )
				? sanitize_text_field( wp_unslash( $_SERVER['HTTP_SIGNATURE_AGENT'] ) )
				: '';

			// The signature headers must accompany Signature-Agent per the
			// spec; without them the header is meaningless noise.
			if ( '' === $agent || ! isset( $_SERVER['HTTP_SIGNATURE'], $_SERVER['HTTP_SIGNATURE_INPUT'] ) ) {
				return null;
			}

			$map = array(
				array( 'chatgpt.com', 'openai' ),
				array( 'openai.com', 'openai' ),
				array( 'claude.ai', 'anthropic' ),
				array( 'anthropic.com', 'anthropic' ),
				array( 'perplexity.ai', 'perplexity' ),
				array( 'google.com', 'google' ),
				array( 'copilot.microsoft.com', 'microsoft' ),
			);

			$host = self::agent_host( $agent );

			foreach ( $map as $entry ) {
				if ( $host === $entry[0] || self::ends_with( $host, '.' . $entry[0] ) ) {
					return $entry[1];
				}
			}

			// Unknown directory: confidently an agent, brand unknown.
			return 'other';
		}

		/**
		 * User-Agent substring signatures for known AI agents and crawlers.
		 *
		 * Strictly known signatures — no generic "looks like a bot"
		 * heuristics; unknown bots are simply not counted. First hit wins.
		 * With the purpose dimension gone, agent tokens fully covered by a
		 * broader same-brand token were dropped (for example
		 * `Claude-SearchBot`, `ChatGPT-User`, `PerplexityBot` — matched by
		 * `Claude`, `ChatGPT`, `Perplexity`). The generic brand tokens stay
		 * last so specific tokens keep winning, as in the source plugin.
		 *
		 * @return string|null Canonical brand, or null when no signature matches.
		 */
		private static function match_user_agent() {
			$user_agent = isset( $_SERVER['HTTP_USER_AGENT'] )
				? sanitize_text_field( wp_unslash( $_SERVER['HTTP_USER_AGENT'] ) )
				: '';

			if ( '' === $user_agent ) {
				return null;
			}

			$table = array(
				// OpenAI.
				array( 'OAI-SearchBot', 'openai' ),
				array( 'GPTBot', 'openai' ),
				// Google (AI-specific agents only; regular Googlebot is out of scope).
				array( 'Google-Extended', 'google' ),
				array( 'Google-CloudVertexBot', 'google' ),
				// Meta.
				array( 'Meta-ExternalAgent', 'meta' ),
				array( 'Meta-ExternalFetcher', 'meta' ),
				array( 'FacebookBot', 'meta' ),
				// Microsoft (AI-specific; regular Bingbot is out of scope).
				array( 'MicrosoftPreview', 'microsoft' ),
				// Amazon.
				array( 'Amazonbot', 'amazon' ),
				array( 'NovaAct', 'amazon' ),
				// Apple (AI-specific opt-out agent; regular Applebot is out of scope).
				array( 'Applebot-Extended', 'apple' ),
				// ByteDance.
				array( 'Bytespider', 'bytedance' ),
				array( 'TikTokSpider', 'bytedance' ),
				// Common Crawl.
				array( 'CCBot', 'commoncrawl' ),
				// Smaller or unbranded AI agents.
				array( 'cohere-training-data-crawler', 'other' ),
				array( 'cohere-ai', 'other' ),
				array( 'Diffbot', 'other' ),
				array( 'Timpibot', 'other' ),
				array( 'omgilibot', 'other' ),
				array( 'omgili', 'other' ),
				array( 'iaskspider', 'other' ),
				array( 'DuckAssistBot', 'other' ),
				array( 'LinerBot', 'other' ),
				array( 'QualifiedBot', 'other' ),
				array( 'PetalBot', 'other' ),
				array( 'AI2Bot', 'other' ),
				array( 'ImagesiftBot', 'other' ),
				array( 'PanguBot', 'other' ),
				array( 'SemrushBot-OCOB', 'other' ),
				array( 'YouBot', 'other' ),
				// Generic brand tokens: match both the official agent UAs
				// (ChatGPT-User, ClaudeBot, PerplexityBot, ...) and
				// semi-custom UAs that still name a known AI vendor.
				array( 'Claude', 'anthropic' ),
				array( 'Anthropic', 'anthropic' ),
				array( 'ChatGPT', 'openai' ),
				array( 'OpenAI', 'openai' ),
				array( 'Gemini', 'google' ),
				array( 'Perplexity', 'perplexity' ),
				array( 'Copilot', 'microsoft' ),
				array( 'Mistral', 'mistral' ),
				array( 'DeepSeek', 'other' ),
			);

			foreach ( $table as $signature ) {
				if ( false !== stripos( $user_agent, $signature[0] ) ) {
					return $signature[1];
				}
			}

			return null;
		}

		/**
		 * AI-referral detection via `utm_source` and the `Referer` header.
		 *
		 * These visits are typically a *human* following a link from an AI
		 * product. Weakest signal, only consulted when the other two miss.
		 *
		 * @return string|null Canonical brand, or null when neither hint matches.
		 */
		private static function match_referral() {
			// phpcs:ignore WordPress.Security.NonceVerification.Recommended -- Read-only analytics hint on a public request; no state change.
			$utm_source = ( isset( $_GET['utm_source'] ) && is_string( $_GET['utm_source'] ) )
				// phpcs:ignore WordPress.Security.NonceVerification.Recommended -- See above.
				? strtolower( trim( sanitize_text_field( wp_unslash( $_GET['utm_source'] ) ) ) )
				: '';

			$referer_host = '';

			if ( isset( $_SERVER['HTTP_REFERER'] ) ) {
				$host         = wp_parse_url( sanitize_text_field( wp_unslash( $_SERVER['HTTP_REFERER'] ) ), PHP_URL_HOST );
				$referer_host = is_string( $host ) ? strtolower( $host ) : '';
			}

			if ( '' === $utm_source && '' === $referer_host ) {
				return null;
			}

			$map = array(
				array(
					'brand'   => 'openai',
					'sources' => array( 'chatgpt.com', 'chatgpt', 'openai', 'openai.com' ),
					'hosts'   => array( 'chatgpt.com', 'chat.openai.com' ),
				),
				array(
					'brand'   => 'anthropic',
					'sources' => array( 'claude.ai', 'claude', 'anthropic' ),
					'hosts'   => array( 'claude.ai' ),
				),
				array(
					'brand'   => 'perplexity',
					'sources' => array( 'perplexity.ai', 'perplexity' ),
					'hosts'   => array( 'perplexity.ai' ),
				),
				array(
					'brand'   => 'google',
					'sources' => array( 'gemini.google.com', 'gemini' ),
					'hosts'   => array( 'gemini.google.com', 'bard.google.com' ),
				),
				array(
					'brand'   => 'microsoft',
					'sources' => array( 'copilot.microsoft.com', 'copilot', 'bing-copilot' ),
					'hosts'   => array( 'copilot.microsoft.com' ),
				),
				array(
					'brand'   => 'meta',
					'sources' => array( 'meta.ai' ),
					'hosts'   => array( 'meta.ai', 'www.meta.ai' ),
				),
				array(
					'brand'   => 'other',
					'sources' => array( 'grok.com', 'grok', 'deepseek', 'mistral', 'lechat' ),
					'hosts'   => array( 'grok.com', 'chat.deepseek.com', 'chat.mistral.ai', 'you.com', 'duckduckgo.com' ),
				),
			);

			foreach ( $map as $entry ) {
				if ( '' !== $utm_source && in_array( $utm_source, $entry['sources'], true ) ) {
					return $entry['brand'];
				}

				if ( '' !== $referer_host && self::host_matches( $referer_host, $entry['hosts'] ) ) {
					return $entry['brand'];
				}
			}

			return null;
		}

		/**
		 * Extract the host from a Signature-Agent value.
		 *
		 * The header value is an sf-string, typically a quoted https URL or
		 * bare host, for example `"https://chatgpt.com"`.
		 *
		 * @param string $agent Raw header value.
		 * @return string Lower-cased host, or '' when unparseable.
		 */
		private static function agent_host( $agent ) {
			$agent = trim( trim( $agent ), '"' );

			if ( '' === $agent ) {
				return '';
			}

			if ( false === strpos( $agent, '//' ) ) {
				$agent = 'https://' . $agent;
			}

			$host = wp_parse_url( $agent, PHP_URL_HOST );

			return is_string( $host ) ? strtolower( $host ) : '';
		}

		/**
		 * Whether a referrer host matches a host list (including subdomains).
		 *
		 * @param string $host  Lower-cased referrer host.
		 * @param array  $hosts Candidate hosts.
		 * @return bool
		 */
		private static function host_matches( $host, $hosts ) {
			foreach ( $hosts as $candidate ) {
				if ( $host === $candidate || self::ends_with( $host, '.' . $candidate ) ) {
					return true;
				}
			}

			return false;
		}

		/**
		 * PHP 7.4-compatible str_ends_with().
		 *
		 * @param string $haystack Haystack.
		 * @param string $needle   Needle.
		 * @return bool
		 */
		private static function ends_with( $haystack, $needle ) {
			$length = strlen( $needle );

			return $length > 0 && substr( $haystack, -$length ) === $needle;
		}
	}

	WCUS_Agentic_Product_Visits::register();
}
