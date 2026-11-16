import { useEffect, useState } from "react";
import type { ChangeEvent, FormEvent, ReactNode } from "react";
import { createRoot } from "react-dom/client";
import sampleInvoice from "../../samples/invoice.txt?raw";
import "./style.css";

type Item = { description: string | null; quantity: string | null; unit_price: string | null; amount: string | null };
type Invoice = {
  supplier: string | null; invoice_number: string | null; invoice_date: string | null;
  currency: string | null; subtotal: string | null; tax: string | null; total: string | null; items: Item[];
};
type Document = {
  id: string; status: "needs_review" | "validated"; filename: string; invoice: Invoice; version: number;
  warnings: string[]; audit: { action: string; at: string }[]; note: string | null; created_at: string;
};

const textFields = [
  ["supplier", "Supplier"], ["invoice_number", "Invoice number"], ["invoice_date", "Invoice date"]
] as const;
const totals = [["subtotal", "Subtotal"], ["tax", "Tax"], ["total", "Total"]] as const;
const itemFields = [
  ["description", "Description"], ["quantity", "Qty"], ["unit_price", "Unit price"], ["amount", "Amount"]
] as const;
const money = ["EUR", "USD", "GBP", "TRY", "CAD", "AUD"];
const date = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" });
const newItem: Item = { description: null, quantity: null, unit_price: null, amount: null };

class RequestError extends Error {
  constructor(message: string, public readonly issues: string[] = []) {
    super(message);
  }
}

async function request(path: string, token: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch("/api/" + path, { ...init, headers: { Authorization: "Bearer " + token, ...init.headers } });
  if (!res.ok) {
    const { detail } = await res.json().catch(() => ({ detail: undefined }));
    const validation = Array.isArray(detail) ? detail.map(({ loc, msg }) => loc.slice(1).join(".") + " · " + msg) : detail?.issues;
    throw new RequestError(typeof detail === "string" ? detail : "Check the invoice fields and try again.", validation);
  }
  return res;
}

function Status({ item }: { item: Document }) {
  return <span className={"status " + item.status}>{item.status === "validated" ? "Validated" : "Needs review"}</span>;
}

function App() {
  const [token, setToken] = useState("");
  const [credential, setCredential] = useState("");
  const [demo, setDemo] = useState(false);
  const [connecting, setConnecting] = useState(true);
  const [busy, setBusy] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [selected, setSelected] = useState<Document | null>(null);
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [issues, setIssues] = useState<string[]>([]);

  function select(item: Document) {
    setSelected(item);
    setInvoice(item.invoice);
    setIssues(item.warnings);
    setError("");
  }

  async function connect(value: string) {
    setConnecting(true);
    setError("");
    try {
      const response = await request("documents", value);
      const existing: Document[] = await response.json();
      setToken(value);
      setDocuments(existing);
      if (existing[0]) select(existing[0]);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setConnecting(false);
    }
  }

  useEffect(() => {
    async function start() {
      try {
        const response = await fetch("/api/config");
        if (!response.ok) throw new Error("The DocFlow server is unavailable.");
        const config = await response.json();
        setDemo(config.demo_access);
        if (config.demo_access) {
          await connect("docflow-local-demo");
          return;
        }
      } catch {
        setError("The DocFlow server is unavailable.");
      }
      setConnecting(false);
    }
    void start();
  }, []);

  async function run(label: string, work: () => Promise<void>) {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await work();
    } catch (reason) {
      setError((reason as Error).message);
      if (reason instanceof RequestError && reason.issues.length) setIssues(reason.issues);
    } finally {
      setBusy("");
    }
  }

  async function upload(name: string, blob: Blob, mediaType: string) {
    const response = await request("documents", token, {
      method: "POST",
      body: blob,
      headers: { "Content-Type": mediaType, "X-Filename": encodeURIComponent(name) }
    });
    const item: Document & { duplicate: boolean } = await response.json();
    setDocuments(previous => item.duplicate && previous.some(other => other.id === item.id)
      ? previous.map(other => other.id === item.id ? item : other) : [item, ...previous]);
    select(item);
    setNotice(item.duplicate ? "Opened the existing document." : "Invoice fields extracted.");
  }

  function loadSample() {
    const number = "AF-" + crypto.randomUUID().slice(0, 8).toUpperCase();
    const sample = sampleInvoice.replace(/^Invoice Number: .*$/m, "Invoice Number: " + number);
    return upload("amber-field-invoice.txt", new Blob([sample]), "text/plain");
  }

  function readFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!/\.(pdf|txt)$/i.test(file.name)) {
      setError("Choose a PDF or UTF-8 text invoice.");
      return;
    }
    void run("Extracting", () => upload(file.name, file, file.name.toLowerCase().endsWith(".pdf") ? "application/pdf" : "text/plain"));
  }

  async function validate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !invoice) return;
    await run("Validating", async () => {
      const response = await request("documents/" + selected.id + "/review", token, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invoice, version: selected.version })
      });
      const item: Document = await response.json();
      setDocuments(previous => previous.map(document => document.id === item.id ? item : document));
      select(item);
      setNotice("Invoice validated. Exports are available.");
    });
  }

  async function download(format: "json" | "csv") {
    if (!selected) return;
    await run("Preparing download", async () => {
      const response = await request("documents/" + selected.id + "/export?format=" + format, token);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "invoice-" + (selected.invoice.invoice_number || selected.id).replace(/[^a-zA-Z0-9-]/g, "-") + "." + format;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    });
  }

  function field(name: Exclude<keyof Invoice, "items">, label: string, type = "text"): ReactNode {
    return <label key={name}><span>{label}</span><input type={type} value={invoice?.[name] ?? ""} onChange={event => {
      const value = event.target.value || null;
      setInvoice(previous => previous && { ...previous, [name]: value });
    }} /></label>;
  }

  return <>
    <header className="masthead">
      <div className="brand"><span aria-hidden="true">▤</span>DocFlow</div>
      <span className="mast-title">Document operations</span>
      {token && <button className="top-action" onClick={() => {
        setToken(""); setDocuments([]); setSelected(null); setInvoice(null); setError(""); setIssues([]); setNotice("");
      }} disabled={!!busy}>Sign out</button>}
    </header>
    <main>
      <div className="intro">
        <div><h1>Invoice intake</h1><p>Extract invoice fields, resolve validation issues and export approved data.</p></div>
        {token && <div className="actions">
          {demo && <button onClick={() => void run("Extracting", loadSample)} disabled={!!busy}>Load sample</button>}
          <label className={"primary button" + (busy ? " disabled" : "")}>Upload invoice<input type="file" accept=".pdf,.txt,application/pdf,text/plain" onChange={readFile} disabled={!!busy} /></label>
        </div>}
      </div>

      {error && <p className="error" role="alert">{error}</p>}
      {(notice || busy) && <p className="notice" role="status">{busy ? busy + "…" : notice}</p>}
      {!token ? <section className="card login">
        {connecting ? <p role="status">Connecting…</p> : <form onSubmit={event => { event.preventDefault(); void connect(credential.trim()); setCredential(""); }}>
          <h2>Operator access</h2>
          <label><span>Operator token</span><input type="password" value={credential} onChange={event => setCredential(event.target.value)} required /></label>
          <button type="submit" className="primary">Connect</button>
        </form>}
      </section> : <div className="workspace">
        <aside className="card queue" aria-label="Review queue">
          <div className="card-heading"><h2>Review queue</h2><span>{documents.length}</span></div>
          {!documents.length && <p className="empty">Uploaded invoices appear here.</p>}
          <ul>{documents.map(item => <li key={item.id}>
            <button type="button" aria-pressed={item.id === selected?.id} disabled={!!busy} onClick={() => select(item)}>
              <strong>{item.invoice.supplier ?? item.filename}</strong><small>{item.invoice.invoice_number ?? item.filename}</small>
              <Status item={item} />
            </button>
          </li>)}</ul>
        </aside>

        <section className="card review" aria-label="Invoice review">
          <div className="card-heading review-heading"><div><h2>Invoice review</h2>{selected && <p>{selected.filename} · {date.format(new Date(selected.created_at))}</p>}</div>{selected && <Status item={selected} />}</div>
          {!selected || !invoice ? <p className="empty">Upload an invoice to review its extracted fields.</p> :
            <div className="paper">
              {!!issues.length && <div className="warnings"><h3>Validation issues</h3><ul>{issues.map(issue => <li key={issue}>{issue}</li>)}</ul></div>}
              {selected.note && <p className="model-note"><strong>Model review note</strong> {selected.note}</p>}
              <form onSubmit={validate}>
                <fieldset disabled={!!busy || selected.status === "validated"}>
                  <legend>Invoice details</legend>
                  <div className="fields">
                    {textFields.map(([key, label]) => field(key, label, key === "invoice_date" ? "date" : "text"))}
                    <label><span>Currency</span><select value={invoice.currency ?? ""} onChange={event => {
                      const value = event.target.value || null;
                      setInvoice(previous => previous && { ...previous, currency: value });
                    }}><option value="">Choose currency</option>{money.map(code => <option key={code}>{code}</option>)}</select></label>
                  </div>
                  <h3>Line items</h3>
                  <div className="table-scroll">
                    <table><thead><tr>{itemFields.map(([key, label]) => <th key={key} scope="col">{label}</th>)}{selected.status === "needs_review" && <th scope="col">Remove</th>}</tr></thead>
                      <tbody>{invoice.items.map((item, index) => <tr key={index}>
                        {itemFields.map(([key, label]) => <td key={key}>
                          <input aria-label={label + " " + (index + 1)} value={item[key] ?? ""} inputMode={key === "description" ? "text" : "decimal"} onChange={event => {
                            const value = event.target.value || null;
                            setInvoice(previous => previous && { ...previous, items: previous.items.map((row, at) => at === index ? { ...row, [key]: value } : row) });
                          }} />
                        </td>)}
                        {selected.status === "needs_review" && <td><button className="remove" type="button" aria-label={"Remove line " + (index + 1)} onClick={() => setInvoice(previous => previous && { ...previous, items: previous.items.filter((_, at) => at !== index) })}>×</button></td>}
                      </tr>)}</tbody>
                    </table>
                  </div>
                  {selected.status === "needs_review" && <button className="add-line" type="button" disabled={invoice.items.length >= 60} onClick={() => setInvoice(previous => previous && { ...previous, items: [...previous.items, newItem] })}>Add line item</button>}
                  <div className="fields totals">{totals.map(([key, label]) => field(key, label))}</div>
                  {selected.status === "needs_review" && <button type="submit" className="primary">Validate invoice</button>}
                </fieldset>
              </form>
              {selected.status === "validated" && <div className="exports"><button onClick={() => void download("json")} disabled={!!busy}>Export JSON</button><button className="primary" onClick={() => void download("csv")} disabled={!!busy}>Export CSV</button></div>}
              <div className="history"><h3>Audit history</h3>{selected.audit.map((entry, index) => <p key={index}>{entry.action} <time dateTime={entry.at}>{date.format(new Date(entry.at))}</time></p>)}</div>
            </div>}
        </section>
      </div>}
    </main>
  </>;
}

const root = document.getElementById("root");
if (!root) throw new Error("DocFlow root element is missing.");
createRoot(root).render(<App />);
