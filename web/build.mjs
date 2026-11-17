import { build, context } from "esbuild";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createServer, request } from "node:http";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const watch = process.argv.includes("--watch");
const options = {
  absWorkingDir: root,
  entryPoints: ["src/main.tsx"],
  bundle: true,
  format: "esm",
  jsx: "automatic",
  target: "es2022",
  outdir: "dist/assets",
  loader: { ".txt": "text" },
  define: { "process.env.NODE_ENV": JSON.stringify(watch ? "development" : "production") },
  minify: !watch,
  logLevel: "info"
};
await mkdir(new URL("dist", import.meta.url), { recursive: true });
const html = (await readFile(new URL("index.html", import.meta.url), "utf8"))
  .replace("/src/main.tsx", "/assets/main.js")
  .replace("</head>", '  <link rel="stylesheet" href="/assets/main.css">\n  </head>');
await writeFile(new URL("dist/index.html", import.meta.url), html);

if (!watch) {
  await build(options);
} else {
  const compiler = await context(options);
  await compiler.watch();
  const assets = await compiler.serve({ servedir: "dist", host: "127.0.0.1", port: 0 });
  const server = createServer((incoming, outgoing) => {
    const targetPort = incoming.url?.startsWith("/api/") ? 8012 : assets.port;
    const upstream = request({ hostname: "127.0.0.1", port: targetPort, path: incoming.url,
      method: incoming.method, headers: { ...incoming.headers, host: `127.0.0.1:${targetPort}` },
      timeout: 60_000 }, response => {
      outgoing.writeHead(response.statusCode ?? 502, response.headers);
      response.pipe(outgoing);
    });
    upstream.on("timeout", () => upstream.destroy());
    upstream.on("error", () => {
      if (!outgoing.headersSent) outgoing.writeHead(502, { "Content-Type": "text/plain" });
      outgoing.end("The development backend is unavailable.");
    });
    incoming.on("aborted", () => upstream.destroy());
    incoming.pipe(upstream);
  });
  server.listen(5173, "127.0.0.1", () => console.log("DocFlow development: http://127.0.0.1:5173; refresh after edits."));
  const stop = () => { server.close(); void compiler.dispose().then(() => process.exit(0)); };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
}
