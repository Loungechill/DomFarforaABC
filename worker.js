const FEED_URL =
  "https://github.com/Loungechill/DomFarforaABC/releases/download/feed-latest/feed.xml";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", {
        status: 405,
        headers: { Allow: "GET, HEAD" },
      });
    }

    if (url.pathname !== "/" && url.pathname !== "/feed.xml") {
      return new Response("Not Found", { status: 404 });
    }

    const cache = caches.default;
    const cacheKey = new Request(`${url.origin}/feed.xml`, { method: "GET" });
    let response = await cache.match(cacheKey);

    if (!response) {
      const upstream = await fetch(FEED_URL, {
        redirect: "follow",
        cf: {
          cacheEverything: true,
          cacheTtl: 300,
        },
      });

      if (!upstream.ok) {
        return new Response("Feed is temporarily unavailable", { status: 502 });
      }

      const headers = new Headers(upstream.headers);
      headers.set("Content-Type", "application/xml; charset=utf-8");
      headers.set("Cache-Control", "public, max-age=300");
      headers.set("Access-Control-Allow-Origin", "*");
      headers.delete("Content-Disposition");

      response = new Response(upstream.body, {
        status: 200,
        headers,
      });
      ctx.waitUntil(cache.put(cacheKey, response.clone()));
    }

    if (request.method === "HEAD") {
      return new Response(null, {
        status: response.status,
        headers: response.headers,
      });
    }

    return response;
  },
};

