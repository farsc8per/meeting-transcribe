// Just enough markdown for the documents you actually work in: headings, lists
// (nested, and with task boxes), tables, code, emphasis, links, quotes, rules.
// Deliberately not a full parser and deliberately not a CDN dependency — this app is
// offline-first and localhost-only.
//
// Escaping happens FIRST, on the raw text, so nothing in a transcript-derived summary can
// inject markup. Everything after that only ever adds tags of our own. Do not reorder it.
function mdToHtml(src){
  // Quotes are escaped too, so nothing can break out of the href="" below.
  const esc = t => t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
                    .replace(/"/g,"&quot;");
  // Only web, mail and relative targets become links; `javascript:` and friends stay text.
  const safeHref = h => /^(https?:|mailto:|\/|#|\.)/i.test(h) || !/^[a-z][a-z0-9+.-]*:/i.test(h);
  const inline = t => esc(t)
    .replace(/`([^`]+)`/g, (m,a)=>"<code>"+a+"</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    // [[wiki-links]] between memory files. Rendered as an inert anchor; the page that
    // knows the names (files.js) resolves and wires them after rendering.
    .replace(/\[\[([A-Za-z0-9_.\-]+)\]\]/g, '<a class="wl" href="#" data-wl="$1">$1</a>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m,txt,h) => safeHref(h)
      ? '<a href="'+h+'" target="_blank" rel="noopener">'+txt+'</a>' : m);

  const out = [];
  const lines = String(src||"").replace(/\r\n?/g,"\n").split("\n");
  let i = 0, inCode = false, code = [];

  const flushCode = () => { out.push("<pre><code>"+esc(code.join("\n"))+"</code></pre>"); code=[]; };

  const BULLET = /^(\s*)[-*+]\s+(.*)$/;
  const NUMBER = /^(\s*)\d+[.)]\s+(.*)$/;

  // A list item's body, with `- [ ]` / `- [x]` turned into a real (disabled) checkbox.
  // The marker is matched on the RAW text before escaping, then stripped, so the box is
  // ours and the remaining text still goes through inline() untouched.
  function itemHtml(txt){
    const m = txt.match(/^\[([ xX])\]\s+(.*)$/);
    if(!m) return inline(txt);
    const done = m[1] !== " ";
    return '<label class="task"><input type="checkbox" disabled'
         + (done ? " checked" : "") + "> " + inline(m[2]) + "</label>";
  }

  // Nested lists are built from the leading indent. A deeper item is emitted INSIDE the
  // <li> above it, which is what makes a two-level recap render as two levels instead of
  // one flat run.
  function buildList(items, i, indent){
    const tag = items[i].ordered ? "ol" : "ul";
    out.push("<"+tag+">");
    while(i < items.length && items[i].indent >= indent){
      if(items[i].indent > indent){ i = buildList(items, i, items[i].indent); continue; }
      out.push("<li>"+itemHtml(items[i].text));
      if(i+1 < items.length && items[i+1].indent > indent){
        i = buildList(items, i+1, items[i+1].indent);
      } else { i++; }
      out.push("</li>");
    }
    out.push("</"+tag+">");
    return i;
  }

  while(i < lines.length){
    const ln = lines[i];
    const startedAt = i;          // see the progress guard at the bottom of this loop

    if(/^```/.test(ln)){
      if(inCode){ flushCode(); inCode=false; } else inCode=true;
      i++; continue;
    }
    if(inCode){ code.push(ln); i++; continue; }

    if(/^\s*$/.test(ln)){ i++; continue; }
    if(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(ln)){ out.push("<hr>"); i++; continue; }

    const h = ln.match(/^(#{1,6})\s+(.*)$/);
    if(h){ const n=h[1].length; out.push(`<h${n}>`+inline(h[2])+`</h${n}>`); i++; continue; }

    // table: a header row followed by a |---|---| separator
    if(/^\s*\|/.test(ln) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i+1]||"")){
      const cells = r => r.trim().replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
      const head = cells(ln);
      i += 2;
      const body = [];
      while(i < lines.length && /^\s*\|/.test(lines[i])){ body.push(cells(lines[i])); i++; }
      out.push("<table><thead><tr>" + head.map(c=>"<th>"+inline(c)+"</th>").join("") +
               "</tr></thead><tbody>" +
               body.map(r=>"<tr>"+r.map(c=>"<td>"+inline(c)+"</td>").join("")+"</tr>").join("") +
               "</tbody></table>");
      continue;
    }

    if(/^\s*>/.test(ln)){
      const buf=[];
      while(i<lines.length && /^\s*>/.test(lines[i])){ buf.push(lines[i].replace(/^\s*>\s?/,"")); i++; }
      out.push("<blockquote>"+inline(buf.join(" "))+"</blockquote>");
      continue;
    }

    if(BULLET.test(ln) || NUMBER.test(ln)){
      const items = [];
      while(i < lines.length){
        const b = lines[i].match(BULLET), n = lines[i].match(NUMBER);
        if(!b && !n) break;
        const m = b || n;
        items.push({ indent: m[1].replace(/\t/g,"  ").length,
                     ordered: !b, text: m[2] });
        i++;
      }
      buildList(items, 0, items[0].indent);
      continue;
    }

    const para=[];
    while(i<lines.length && !/^\s*$/.test(lines[i]) && !BULLET.test(lines[i])
          && !NUMBER.test(lines[i]) && !/^#{1,6}\s/.test(lines[i])
          && !/^\s*\|/.test(lines[i]) && !/^```/.test(lines[i]) && !/^\s*>/.test(lines[i])){
      para.push(lines[i]); i++;
    }
    // Join with <br>, not a space. Strict markdown folds a single newline into a space,
    // but these summaries are written one line per fact ("**Date:** …" / "**Attendees:** …")
    // and long prose is never hard-wrapped, so folding them ran the header block together.
    if(para.length) out.push("<p>"+para.map(inline).join("<br>")+"</p>");

    // PROGRESS GUARD. Without this the parser can spin forever, and it did: a line that
    // opens with "|" but is NOT followed by a |---|---| separator row falls past the
    // table branch, and then the paragraph loop refuses it too (its terminator excludes
    // lines starting with "|"). `para` comes back empty, nothing is emitted, `i` never
    // moves. A single stray pipe in a recap froze the tab.
    //
    // Fixed at the source — an unconsumed line is emitted as an ordinary paragraph — and
    // backstopped here, so any future branch that forgets to advance costs a stray line
    // rather than a hung browser.
    if(i === startedAt){
      out.push("<p>"+inline(lines[i])+"</p>");
      i++;
    }
  }
  if(inCode && code.length) flushCode();
  return out.join("\n");
}
