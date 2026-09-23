"""Extract OMML formulas from the official problem .docx as linear text.

Read-only on the official source. Writes nothing unless --out is given.
"""
import sys
import zipfile
import xml.etree.ElementTree as ET

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

GREEK = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "η": r"\eta", "θ": r"\theta", "λ": r"\lambda", "μ": r"\mu",
    "π": r"\pi", "ρ": r"\rho", "σ": r"\sigma", "τ": r"\tau",
    "φ": r"\phi", "ω": r"\omega", "Δ": r"\Delta", "Σ": r"\Sigma",
    "≤": r"\le ", "≥": r"\ge ", "≠": r"\ne ", "∈": r"\in ", "∉": r"\notin ",
    "·": r"\cdot ", "×": r"\times ", "→": r"\to ", "−": "-",
    "∞": r"\infty ", "∑": r"\sum ", "√": r"\sqrt ",
}


def esc(s):
    for k, v in GREEK.items():
        s = s.replace(k, v + " " if v.endswith(" ") or v.endswith("}") else v + " ")
    return s


def txt(el):
    """Plain text of a subtree. OMML runs use m:t, prose runs use w:t."""
    out = []
    for t in el.iter():
        if t.tag in (W + "t", M + "t"):
            out.append(t.text or "")
    return "".join(out)


def conv(el):
    """Recursive OMML -> linear text."""
    tag = el.tag
    if tag == M + "f":                      # fraction
        num = den = ""
        for c in el:
            if c.tag == M + "num":
                num = conv(c)
            elif c.tag == M + "den":
                den = conv(c)
        return f"(({num})/({den}))"
    if tag == M + "rad":                    # radical
        deg = e = ""
        for c in el:
            if c.tag == M + "deg":
                deg = conv(c)
            elif c.tag == M + "e":
                e = conv(c)
        return f"\\sqrt[{deg}]{{{e}}}" if deg else f"\\sqrt{{{e}}}"
    if tag in (M + "sSup", M + "sSub", M + "sSubSup"):
        base = sup = sub = ""
        for c in el:
            if c.tag == M + "e":
                base = conv(c)
            elif c.tag == M + "sup":
                sup = conv(c)
            elif c.tag == M + "sub":
                sub = conv(c)
        if tag == M + "sSup":
            return f"{base}^({sup})"
        if tag == M + "sSub":
            return f"{base}_({sub})"
        return f"{base}_({sub})^({sup})"
    if tag == M + "d":                      # delimiter
        inner = "".join(conv(c) for c in el if c.tag == M + "e")
        beg, end = "(", ")"
        for c in el:
            if c.tag == M + "dPr":
                for p in c:
                    if p.tag == M + "begChr":
                        beg = p.get(M + "val", "(")
                    if p.tag == M + "endChr":
                        end = p.get(M + "val", ")")
        return f"{beg}{inner}{end}"
    if tag == M + "nary":                   # sum / integral
        sub = sup = e = chr = ""
        for c in el:
            if c.tag == M + "naryPr":
                for p in c:
                    if p.tag == M + "chr":
                        chr = p.get(M + "val", "∑")
            elif c.tag == M + "sub":
                sub = conv(c)
            elif c.tag == M + "sup":
                sup = conv(c)
            elif c.tag == M + "e":
                e = conv(c)
        s = chr
        if sub:
            s += f"_({sub})"
        if sup:
            s += f"^({sup})"
        return f"{s} {e}"
    if tag == M + "func":
        name = arg = ""
        for c in el:
            if c.tag == M + "fName":
                name = conv(c)
            elif c.tag == M + "e":
                arg = conv(c)
        return f"{name}({arg})"
    if tag == M + "acc":                    # accent
        base = ""
        for c in el:
            if c.tag == M + "e":
                base = conv(c)
        return f"\\bar{{{base}}}"
    if tag == M + "m":                      # matrix
        rows = []
        for mr in el.iter(M + "mr"):
            rows.append(" , ".join(conv(c) for c in mr if c.tag == M + "e"))
        return "[ " + " ; ".join(rows) + " ]"
    if tag in (M + "oMath", M + "oMathPara", M + "e", M + "num", M + "den",
               M + "sub", M + "sup", M + "deg", M + "fName", M + "r"):
        return "".join(conv(c) for c in el)
    if tag in (M + "rPr", M + "ctrlPr", M + "t"):
        return txt(el)
    if tag.startswith(W):
        return txt(el)
    return "".join(conv(c) for c in el)


def main():
    path = sys.argv[1]
    out_path = None
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    lines = []
    n = 0
    for child in body.iter():
        if child.tag == W + "p":
            buf = []
            for el in child.iter():
                if el.tag == M + "oMath":
                    n += 1
                    buf.append(f"  <<EQ{n}: {esc(conv(el))}>>")
                elif el.tag == W + "t":
                    buf.append(el.text or "")
            line = "".join(buf).strip()
            if line:
                lines.append(line)
    text = "\n".join(lines)
    print(f"parsed {n} oMath elements, {len(lines)} non-empty paragraphs")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("written:", out_path)
    else:
        for ln in lines:
            if "<<EQ" in ln:
                print(ln)


if __name__ == "__main__":
    main()
