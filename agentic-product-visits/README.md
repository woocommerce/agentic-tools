# Agentic Product Visits

A single-file WordPress snippet that counts **AI-agent visits to WooCommerce product pages**, broken down by AI brand.

Every detected visit is attributed to a brand (`openai`, `anthropic`, `google`, etc.) and stored as a counter in the product's post meta. Nothing is added to the admin UI; the data is exposed through two filters.

## Requirements

- WordPress with WooCommerce active.
- PHP 7.4 or newer.

## Installation

Either:

- Paste `agentic-product-visits.php` (without the opening `<?php`) into the **Code Snippets** plugin, set to *run everywhere*; or
- `require` the file from your theme's `functions.php` or a site plugin.

## Reading the data

Both filters return `array<string,int>` mapping brand to visit count. Use `array_sum()` to calculate the total.

```php
// One product.
$stats = apply_filters( 'wcus_agentic_product_visits', array(), $product_id );
// => array( 'openai' => 7, 'anthropic' => 5, 'other' => 2 )

$total = array_sum( $stats ); // 14

// Sitewide aggregate across all products.
$stats = apply_filters( 'wcus_agentic_visits_totals', array() );
```

Example: a per-brand breakdown in a meta box on the product edit screen.

```php
add_action( 'add_meta_boxes_product', function ( $post ) {
	add_meta_box(
		'wcus-agentic-visits',
		'AI agent visits',
		function ( $post ) {
			$stats = apply_filters( 'wcus_agentic_product_visits', array(), $post->ID );

			if ( ! $stats ) {
				echo '<p>No AI agent visits recorded yet.</p>';
				return;
			}

			arsort( $stats );

			echo '<ul>';
			foreach ( $stats as $brand => $visits ) {
				printf( '<li>%s: %d</li>', esc_html( $brand ), (int) $visits );
			}
			printf( '<li><strong>Total: %d</strong></li>', (int) array_sum( $stats ) );
			echo '</ul>';
		},
		'product',
		'side'
	);
} );
```

## How detection works

On `template_redirect` (priority 0), for single product views only, three signals are evaluated in decreasing order of reliability. The first conclusive hit wins:

1. Web Bot Auth headers: `Signature-Agent`, together with `Signature` and `Signature-Input`. The host in `Signature-Agent` maps to a brand. Presence-only: the signature is **not** cryptographically verified, and is trusted no more than a self-declared User-Agent.
2. User-Agent signatures: a table of known AI agent and crawler tokens (`GPTBot`, `ClaudeBot`, `Bytespider`, `CCBot`), with generic brand tokens (`Claude`, `ChatGPT`, `Gemini`) checked last, letting specific tokens win. Only known signatures match. There are no "looks like a bot" heuristics, and unrecognized bots go uncounted.
3. AI referral hints: the `utm_source` query arg and the `Referer` host (`chatgpt.com`, `claude.ai`, `perplexity.ai`). This is the weakest signal, and usually indicates a *human* arriving from an AI product rather than an agent.

Requests are skipped for cron, WP-CLI, and XML-RPC, and for logged-in users who can `edit_posts`. That keeps staff testing with spoofed user agents out of the counts.

Minor agents, and visitors that are confidently agents but of undeterminable brand, land in the `other` bucket.

## Storage

Each brand gets its own flat meta key on the product:

```
_wcus_agentic_visits_brand_openai    => 7
_wcus_agentic_visits_brand_anthropic => 5
_wcus_agentic_visits_brand_other     => 2
```

Flat keys, rather than one serialized array, allow the counter to be incremented atomically with a direct `meta_value + 1` UPDATE, so concurrent hits cannot overwrite each other. Only the very first hit for a brand falls back to `add_post_meta()` to seed the row.

## Resetting the counts

There is no reset UI. Delete the meta rows directly, matching the shared key prefix:

```sql
-- Every product.
DELETE FROM wp_postmeta WHERE meta_key LIKE '\_wcus\_agentic\_visits\_brand\_%';

-- One product.
DELETE FROM wp_postmeta WHERE post_id = 123 AND meta_key LIKE '\_wcus\_agentic\_visits\_brand\_%';
```

If a persistent object cache is in use, flush it afterwards (`wp cache flush`), since deleting rows directly leaves the old values cached.

## Caveats

- Counts have no time dimension. Only a cumulative per-brand total is stored, and a per-day or per-month breakdown is not available. Take periodic snapshots externally if you need trends.
- Full-page caches (server-side or CDN) serve product pages without running PHP, and those visits go uncounted.
- All three signals are self-declared and can be forged. The resulting numbers are approximate.
- The agent and referral tables are hard-coded. New AI products need a snippet update before they are recognized by name; until then they go uncounted, or into `other` if they send Web Bot Auth headers.