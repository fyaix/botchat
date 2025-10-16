/**
 * Telegram Anonymous Chat Bot - Cloudflare Worker Proxy
 *
 * This worker acts as a smart proxy in front of the main Telegram bot.
 * Its primary roles are:
 * 1. Handling Maintenance Mode: Intercepts all requests when maintenance is on
 *    and returns a maintenance message directly, preventing the bot from being hit.
 * 2. Protecting the Bot's URL: The actual bot's URL is hidden behind this worker.
 *
 * --- SETUP INSTRUCTIONS ---
 * 1. Deploy this script to a Cloudflare Worker.
 * 2. Create a Cloudflare KV namespace and name it something like `BOT_MAINTENANCE_KV`.
 * 3. Bind this KV namespace to the worker in your `wrangler.toml` or via the Cloudflare dashboard.
 *    - In `wrangler.toml`, add:
 *      [[kv_namespaces]]
 *      binding = "MAINTENANCE_KV"
 *      id = "your_kv_namespace_id_here"
 * 4. Set the `BOT_URL` secret variable in the worker's settings to your actual bot's URL
 *    (e.g., your Railway, Heroku, or personal server URL).
 *    - `npx wrangler secret put BOT_URL`
 * 5. Set your Telegram bot's webhook to this worker's URL.
 *    - `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=<YOUR_WORKER_URL>`
 *
 * --- HOW IT WORKS WITH THE PYTHON BOT ---
 * Your Python bot's /maintenance and /resume commands would need to be updated
 * to use the Cloudflare API to write `true` or `false` to the `maintenance_status` key
 * in the `MAINTENANCE_KV` namespace. This is how the worker knows when to activate the mode.
 */

export default {
  async fetch(request, env, ctx) {
    // env.MAINTENANCE_KV is the binding to your Cloudflare KV Namespace.
    // env.BOT_URL is the secret variable holding your bot's real URL.

    if (!env.MAINTENANCE_KV) {
      return new Response("KV Namespace 'MAINTENANCE_KV' is not bound.", { status: 500 });
    }
    if (!env.BOT_URL) {
      return new Response("Secret 'BOT_URL' is not set.", { status: 500 });
    }

    // Check maintenance status from KV.
    // The key "maintenance_status" should be "true" or "false".
    const isMaintenance = await env.MAINTENANCE_KV.get("maintenance_status");

    if (isMaintenance === "true") {
      // Get the custom maintenance message from KV.
      const message = await env.MAINTENANCE_KV.get("maintenance_message") || "🔧 The bot is currently under maintenance. Please try again later.";

      // When in maintenance, reply directly to Telegram's webhook request.
      // This prevents the main bot from being contacted at all.
      return new Response(JSON.stringify({
        method: 'sendMessage',
        chat_id: (await request.json()).message.chat.id,
        text: message,
      }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // If not in maintenance, act as a proxy and forward the request.
    return await fetch(env.BOT_URL, request);
  },
};