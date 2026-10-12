"""Generates the project synopsis in the KNIT Sultanpur CSE prescribed format."""

from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from pathlib import Path

OUT = Path(__file__).resolve().parent / "PRAVAH_Synopsis.docx"

TITLE = ("PRAVAH: An AI-Powered Decision Support System For Maximizing "
         "Railway Section Throughput")

doc = Document()

# ---------------------------------------------------------------- page setup
sec = doc.sections[0]
sec.top_margin, sec.bottom_margin = Cm(3), Cm(3)
sec.left_margin, sec.right_margin = Cm(4), Cm(2)

normal = doc.styles["Normal"]
normal.font.name = "Times New Roman"
normal.font.size = Pt(12)
normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
pf = normal.paragraph_format
pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
pf.line_spacing = 1.15
pf.space_after = Pt(4)
pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

ALIGN = {
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
}


def para(text="", size=12, bold=False, italic=False, align="justify",
         space_before=0, space_after=6, caps=False):
    p = doc.add_paragraph()
    p.paragraph_format.alignment = ALIGN[align]
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    if text:
        r = p.add_run(text.upper() if caps else text)
        r.font.name = "Times New Roman"
        r.font.size = Pt(size)
        r.bold = bold
        r.italic = italic
        r._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    return p


def heading(text):
    """14 pt, Bold - per the prescribed format."""
    return para(text, size=14, bold=True, align="left", space_before=8, space_after=4)


def subheading(text):
    """12 pt, Bold Italic - per the prescribed format."""
    return para(text, size=12, bold=True, italic=True, align="left",
                space_before=5, space_after=2)


def _listp(text, style, indent):
    p = doc.add_paragraph(style=style)
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.left_indent = Cm(indent)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.15
    r = p.add_run(text)
    r.font.name = "Times New Roman"
    r.font.size = Pt(12)
    r._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    return p


def bullet(text, indent=0.75):
    return _listp(text, "List Bullet", indent)


def numbered(text, indent=0.75):
    return _listp(text, "List Number", indent)


def mono(text, size=9):
    p = doc.add_paragraph()
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(3)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.0
    r = p.add_run(text)
    r.font.name = "Consolas"
    r.font.size = Pt(size)
    return p


def shade(cell, hexcolor):
    el = OxmlElement("w:shd")
    el.set(qn("w:val"), "clear")
    el.set(qn("w:fill"), hexcolor)
    cell._tc.get_or_add_tcPr().append(el)


def table(rows, header=True, widths=None, font=10):
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            c = t.cell(i, j)
            c.text = ""
            p = c.paragraphs[0]
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.line_spacing = 1.0
            p.paragraph_format.alignment = (
                WD_ALIGN_PARAGRAPH.LEFT if j == 0 else WD_ALIGN_PARAGRAPH.CENTER)
            r = p.add_run(str(val))
            r.font.name = "Times New Roman"
            r.font.size = Pt(font)
            r.bold = (i == 0 and header)
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
            if i == 0 and header:
                shade(c, "D9D9D9")
    if widths:
        for j, w in enumerate(widths):
            for row in t.rows:
                row.cells[j].width = Cm(w)
    return t


# ==================================================================== COVER
para("DEPARTMENT OF COMPUTER SCIENCE & ENGINEERING", size=18, bold=True,
     align="center", space_before=0, space_after=10, caps=True)
para("KAMLA NEHRU INSTITUTE OF TECHNOLOGY, SULTANPUR", size=16, bold=True,
     align="center", space_after=24, caps=True)
para("[ INSERT KNIT LOGO HERE ]", size=12, italic=True, align="center", space_after=24)
para("Academic Session: 2026-27", size=14, bold=True, align="center", space_after=18)
para("Project Synopsis", size=14, bold=True, align="center", space_after=6)
para("ON", size=14, bold=True, align="center", space_after=10)
para(TITLE, size=14, bold=True, align="center", space_after=30)

t = doc.add_table(rows=4, cols=2)
t.alignment = WD_TABLE_ALIGNMENT.CENTER
cover_rows = [
    ("Submitted By:", "Under the Guidance of"),
    ("Name: ____________________  (Roll No: __________)", "Guide 1: ____________________"),
    ("Name: ____________________  (Roll No: __________)", "Guide 2: ____________________"),
    ("Name: ____________________  (Roll No: __________)", ""),
]
for i, (a, b) in enumerate(cover_rows):
    for j, val in enumerate((a, b)):
        c = t.cell(i, j)
        c.text = ""
        p = c.paragraphs[0]
        p.paragraph_format.space_after = Pt(10)
        p.paragraph_format.alignment = (WD_ALIGN_PARAGRAPH.LEFT if j == 0
                                        else WD_ALIGN_PARAGRAPH.RIGHT)
        r = p.add_run(val)
        r.font.name = "Times New Roman"
        r.font.size = Pt(12)
        r.bold = (i == 0)

para(space_after=30)
para("Signature of Students                                          Signature of Guide(s)",
     size=12, align="center", space_before=40)

doc.add_page_break()

# ============================================================= 1. TITLE
heading("1. Title of the Project")
para(TITLE, bold=True)
para("Short name: PRAVAH (Sanskrit: “flow”) — Precision Rail Allocation for Vacant Headways.")

# ============================================================= 2. INTRODUCTION
heading("2. Introduction")
para(
    "Indian Railways moves over 1.5 billion tonnes of originating freight each year, and "
    "freight is the principal source of its revenue, yet rail's share of India's freight "
    "movement has declined for decades in favour of road. The National Rail Plan identifies "
    "recovery of this share as a central objective, and line capacity on saturated "
    "mixed-traffic corridors as the principal constraint [1].")
para(
    "The operative measure of capacity is section throughput: the number of trains a stretch "
    "of railway clears in a day. Raising it by building new track is slow and expensive; "
    "raising it by using existing track better is neither, and is the subject of this "
    "project. Preliminary analysis by the authors establishes how much is available. "
    "Ninety-five thousand block-section-level goods movement records for the Waltair "
    "Division of East Coast Railway, covering 1–27 August 2025, were analysed for the "
    "176.83 km Kottavalasa Junction – Palasa section. A freight train completing it takes a "
    "median 10.52 hours — an effective speed of 16.8 km/h — while the median running speed "
    "of the same trains when in motion is 30.8 km/h. Roughly 45 percent of freight transit "
    "time is detention rather than movement, and the section clears about 31 freight trains "
    "per day. Throughput is limited not by the track, but by how it is used.")
para(
    "Those decisions — when to dispatch each freight train, where to hold it, which train "
    "takes precedence — are made in real time by a section controller, and are resolved "
    "greedily and locally from experience. PRAVAH computes them instead, using two separated "
    "components: a machine learning model estimating how long freight movements actually "
    "take, and a constraint programming solver determining the plan that maximises "
    "throughput. Plans are evaluated against the measured baseline above.")

# ============================================================= 3. PROBLEM STATEMENT
heading("3. Problem Statement")
para(
    "To design and implement a decision support system that maximises the throughput of a "
    "railway section — the number of freight trains cleared per day — while minimising their "
    "transit time and satisfying all operational safety constraints.")
para(
    "Given a section modelled as a directed graph of stations and block sections carrying "
    "length, line count, signalling type and permitted speeds; a per-station line inventory "
    "with individual loop and platform lengths; the published timings of scheduled services; "
    "and a set of freight trains with known origin, destination, earliest availability, rake "
    "length and load status — the system determines a dispatch time for each freight train "
    "or a decision not to dispatch it, with the station at which it is held for each "
    "overtake and the duration of each hold, subject to:")
para(
    "block occupancy, permitting at most one train per block under absolute block "
    "signalling; headway, separating successive entries by the minimum the signalling "
    "permits; blocking, under which a train does not release a block until it has entered "
    "the next on its route; hold feasibility, permitting a train to be held only where a "
    "free line at least as long as the rake exists; and service integrity, under which the "
    "published timings of scheduled services are hard constraints never altered. The "
    "objective is lexicographic — maximise freight trains cleared per planning horizon, then "
    "minimise total freight transit time.")

# ============================================================= 4. OBJECTIVES
heading("4. Objectives")
numbered("To construct a validated computational model of a real Indian Railways section "
         "from official infrastructure and schedule records, and to establish a quantitative "
         "throughput baseline from real movement data, so that improvement claims are "
         "measured against actual railway performance rather than simulation output.")
numbered("To develop a machine learning estimator of block traversal time from historical "
         "movement records, and demonstrate that it improves scheduling accuracy over the "
         "constant-speed assumption conventionally used.")
numbered("To formulate freight path allocation as a blocking job-shop scheduling problem and "
         "solve it exactly by constraint programming, maximising trains cleared and reporting "
         "a certified optimality gap, with an independent discrete-event simulator verifying "
         "every plan for safety violations.")
numbered("To deliver an interactive decision support interface presenting the plan as a "
         "time-distance train graph with disruption handling, explanation of each precedence "
         "decision, and evaluation of candidate infrastructure interventions against the "
         "measured throughput baseline.")

# ============================================================= 5. LITERATURE REVIEW
heading("5. Literature Review")
subheading("5.1 Section Capacity and Scheduling Formulations")
para(
    "Burdett and Kozan [6] formalise the result that the throughput of a mixed-traffic "
    "section is a function of the traffic mix and the dispatching policy, not a fixed "
    "property of the infrastructure — the basis for this project, since better operating "
    "decisions then recover capacity without civil works. Mascis and Pacciarelli [2] "
    "established that railway scheduling with block occupancy is naturally a job-shop "
    "problem with blocking and no-wait constraints, capturing the property that a train "
    "cannot release a resource until it has acquired the next; D'Ariano et al. [3] gave a "
    "branch-and-bound algorithm over this representation, realised in ROMA.")
subheading("5.2 Real-Time Rescheduling and Learning-Based Approaches")
para(
    "Törnquist and Persson [5] show that mixed-integer formulations become intractable as "
    "the disturbance horizon grows, because disjunctive ordering constraints must be encoded "
    "through big-M inequalities with weak linear relaxations, and Cacchiani et al. [4] "
    "conclude that deployed systems universally adopt a limited lookahead window under a "
    "strict computational budget. Šemrov et al. [7] apply reinforcement learning to "
    "rescheduling and report performance comparable to heuristic dispatching, but require "
    "extensive training and give no optimality guarantee, while Corman and Meng [8] observe "
    "that the decisive constraint on deployed systems is whether a dispatcher can understand "
    "and override the recommendation. These shape three decisions here: rolling-horizon "
    "re-optimisation under a time budget, an exact method rather than a learned policy for "
    "dispatching, and justification attached to every recommendation.")
subheading("5.3 Research Gap")
para(
    "Exact and heuristic methods for railway scheduling are mature. Largely absent, "
    "particularly in the Indian context, is work combining an exact formulation with "
    "calibration and validation against real block-section-level movement data from the same "
    "section being optimised; prototypes are typically evaluated against their own "
    "simulators, establishing internal consistency but not operational relevance. This "
    "project derives both its running-time parameters and its throughput baseline from real "
    "recorded movements on the section under study.")

# ============================================================= 6. METHODOLOGY
heading("6. Proposed Methodology")
para("Machine learning and constraint programming occupy separate layers and perform "
     "separate functions; neither substitutes for the other.")
mono(
    "  historical movements --> [ AI ] --> traversal times --> [ CP ] --> plan\n"
    "  section graph + service paths ------------------------> [ CP ]      |\n"
    "         [ INTERFACE ] <-- verified plan <-- [ SIMULATOR ] <-----------+")

subheading("6.1 Data Layer")
para(
    "Official extracts are parsed into a normalised section graph: stations become nodes "
    "positioned by mile-post distance, block sections become directed edges carrying length, "
    "line count, signalling type and permitted speeds, and station line inventories supply "
    "the individual length of each loop and platform line, determining where a rake may "
    "physically be held. Speed restrictions apply as edge-level overrides; scheduled "
    "services become fixed occupancy intervals.")

subheading("6.2 Where Artificial Intelligence Is Used, And Why")
para(
    "Artificial intelligence answers one question: how long a given freight movement will "
    "actually take. Freight trains, unlike scheduled services, have no published timings, so "
    "their running times must be estimated before any plan can be built, and a single "
    "constant is demonstrably inadequate. Median freight speed on the study corridor varies "
    "from 13.0 to 52.8 km/h across block sections and differs systematically between loaded "
    "and empty rakes. On the Korukonda–Vizianagaram block a constant of 30 km/h predicts 21 "
    "minutes where the measured median for a loaded rake is 49 minutes. An error of that "
    "size does not merely degrade a plan, it renders it infeasible: the optimiser allocates "
    "a path through a gap the train cannot physically clear.")
para(
    "The estimator is a gradient-boosted regression tree model [10] trained on the 15,449 "
    "movement records on the study corridor, with features comprising block section "
    "identity, wagon type, load status, hour of day, day of week and prevailing traffic "
    "density. It is trained on the earlier part of the observation window and evaluated on "
    "the held-out remainder, with mean absolute error reported against the constant-speed "
    "baseline. To protect feasibility against prediction error, schedules are built on a "
    "conservative upper quantile of the predicted distribution rather than its mean, and the "
    "trade-off between robustness and throughput is reported. The model estimates durations "
    "only; it never selects a dispatching action.")

subheading("6.3 Where Constraint Programming Is Used, And Why")
para(
    "Constraint programming determines the plan itself: which freight trains run, when each "
    "departs, where each is held, and which takes precedence at every conflict point. An "
    "exact method is used rather than a learned policy because these are safety-bearing "
    "decisions requiring a certified guarantee, not a statistical estimate. It is preferred "
    "to mixed-integer programming because railway scheduling is dominated by disjunctive "
    "ordering constraints, which mixed-integer formulations express only through big-M "
    "inequalities with weak linear relaxations — the intractability reported in [5] — "
    "whereas a constraint solver handles them natively.")
para(
    "The model is solved with the CP-SAT solver of Google OR-Tools [9] and follows the "
    "blocking job-shop structure of [2]. For each train t and resource r on its route an "
    "interval variable is defined, with freight intervals made optional under a presence "
    "literal z[t], so that the number of trains cleared is itself a decision variable:")
mono(
    "Running time   end[t,r] - start[t,r] >= tau(t,r)      (tau from 6.2)\n"
    "Blocking       end[t,r_i] = start[t,r_i+1]\n"
    "Headway        NoOverlap( { occ[t,r] + h(r) : t in T } )      for all r\n"
    "Station lines  Cumulative( { occ[t,s] }, capacity = L(s) )    for all s\n"
    "Hold length    z[t] and hold(t,s) => len(t) <= maxLineLen(s)\n"
    "Service paths  start[p,r] = T_sched(p,r)              for scheduled p\n"
    "OBJECTIVE      maximise SUM z[t], then minimise SUM transit time")
para(
    "Every solution is returned with its optimality gap, so plan quality is known rather "
    "than assumed. For real-time operation the model is re-solved over a rolling horizon of "
    "roughly ninety minutes under a budget of a few seconds, with trains already in motion "
    "fixed in place, following [4] and [5]. The solver reasons over durations given to it; "
    "it never predicts them.")
table([
    ["", "Artificial Intelligence", "Constraint Programming"],
    ["Question", "How long will it take?", "What should be done?"],
    ["Technique", "Gradient-boosted trees", "CP-SAT constraint solver"],
    ["Input", "15,449 past movements", "Graph, paths, predicted times"],
    ["Output", "Block traversal time", "Dispatch, holds, precedence"],
    ["Guarantee", "Error bound on holdout", "Certified optimality gap"],
    ["Runs", "Offline, trained once", "On demand, and on disruption"],
], widths=[3.2, 5.9, 5.9], font=9)

subheading("6.4 Verification and Interface")
para(
    "An independent discrete-event simulator, implemented separately from the solver, "
    "replays each plan and detects headway violations, block conflicts, hold-length "
    "violations and deviations to scheduled services; a plan is accepted only if it reports "
    "zero violations, since a verifier sharing assumptions with the optimiser would confirm "
    "its errors rather than expose them. The plan is presented as an interactive "
    "time-distance train graph, with a comparison view against the baseline, a disruption "
    "console triggering re-optimisation, and an explanation panel giving the constraint "
    "behind each precedence decision and the cost of the alternative.")

# ============================================================= 7. DATASET / TOOLS
heading("7. Dataset / Tools & Technologies")
subheading("7.1 Dataset")
para(
    "The study section is Kottavalasa Junction to Palasa, a 176.83 km multi-line "
    "mixed-traffic corridor on the Howrah–Chennai trunk route, Waltair Division, East Coast "
    "Railway. Official operational extracts [11] are used; counts are distinct entities.")
table([
    ["Dataset", "Count", "Content"],
    ["Block sections", "63", "Length, lines, signalling type, speeds"],
    ["Stations / station lines", "22 / 103", "Position, crossing facility, line lengths"],
    ["Scheduled services / timings", "231 / 3,901", "Per-station arrival, departure, platform"],
    ["Speed restrictions", "9", "Separate passenger and goods limits"],
    ["Goods movements (corridor)", "95,219 (15,449)", "Block-level timestamps, 1-27 Aug 2025"],
], widths=[5.2, 2.3, 7.5], font=9)
para(
    "The corridor goods records cover 1,440 rakes in both directions, supporting both "
    "parameter estimation and the throughput baseline.", space_before=3)

subheading("7.2 Tools and Technologies")
para(
    "Optimisation uses Google OR-Tools CP-SAT; machine learning uses LightGBM with "
    "scikit-learn, pandas and NumPy. The backend and simulator are Python 3.12 with FastAPI "
    "and Pydantic; the interface is React 18 and TypeScript with D3.js rendered to HTML5 "
    "Canvas and Tailwind CSS. PostgreSQL stores scenarios, runs and the audit log, with "
    "Docker Compose, Git and pytest for tooling. Hardware is an x86-64 workstation with at "
    "least 4 physical cores and 16 GB RAM, since CP-SAT parallelises across cores; no GPU is "
    "required.")

# ============================================================= 8. EXPECTED RESULTS
heading("8. Expected Results / Applications")
para("The baseline column is measured from the goods movement dataset; the target column "
     "states the improvement the system aims to demonstrate.", space_after=3)
table([
    ["Performance Indicator", "Measured Baseline", "Target"],
    ["Section throughput (freight cleared per day)", "31.2", ">= 40"],
    ["Median corridor transit time", "10.52 h", "< 7 h"],
    ["Effective end-to-end speed", "16.8 km/h", "> 25 km/h"],
    ["Transit time spent in detention", "45 %", "< 25 %"],
    ["Disturbance to services / safety violations", "—", "zero (enforced)"],
    ["Reported optimality gap", "—", "< 5 %"],
], widths=[8.0, 3.5, 3.5], font=9)
para(
    "The system applies to operational decision support for section controllers; to "
    "infrastructure investment appraisal, quantifying additional throughput per unit of "
    "capital expenditure for works such as new or lengthened loop lines; and to section "
    "capacity assessment. It transfers to any section with equivalent records.",
    space_before=4)
para(
    "The project addresses a single section using offline historical data. It does not "
    "integrate with live signalling, perform network-wide optimisation, address rostering, "
    "or claim safety certification. All recommendations are advisory; the controller remains "
    "the decision-making authority.")

# ============================================================= 9. GANTT
heading("9. Gantt Chart")
para("Indicative twelve-week schedule.", space_after=3)
weeks = ["W{}".format(i) for i in range(1, 13)]
tasks = [
    ("Literature survey and problem formulation",
     [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("Data parsing, section graph, throughput baseline",
     [1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("Discrete-event verification simulator",
     [0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]),
    ("Machine learning traversal time estimator",
     [0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0]),
    ("CP-SAT optimisation model",
     [0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0]),
    ("Decision support interface",
     [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0]),
    ("Disruption handling and infrastructure module",
     [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0]),
    ("Integration, evaluation and documentation",
     [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1]),
]
rows = [["Task"] + weeks] + [[name] + ["" for _ in m] for name, m in tasks]
gt = table(rows, widths=[5.4] + [0.8] * 12, font=8)
for i, (_, marks) in enumerate(tasks, start=1):
    for j, on in enumerate(marks, start=1):
        if on:
            shade(gt.cell(i, j), "404040")

# ============================================================= 10. REFERENCES
heading("10. References")
refs = [
    "Ministry of Railways, Government of India, “National Rail Plan (NRP) – India 2030,” New Delhi, India, 2021.",
    "A. Mascis and D. Pacciarelli, “Job-shop scheduling with blocking and no-wait constraints,” European Journal of Operational Research, vol. 143, no. 3, pp. 498–517, 2002.",
    "A. D’Ariano, D. Pacciarelli, and M. Pranzo, “A branch and bound algorithm for scheduling trains in a railway network,” European Journal of Operational Research, vol. 183, no. 2, pp. 643–657, 2007.",
    "V. Cacchiani, D. Huisman, M. Kidd, L. Kroon, P. Toth, L. Veelenturf, and J. Wagenaar, “An overview of recovery models and algorithms for real-time railway rescheduling,” Transportation Research Part B: Methodological, vol. 63, pp. 15–37, 2014.",
    "J. Törnquist and J. A. Persson, “N-tracked railway traffic re-scheduling during disturbances,” Transportation Research Part B: Methodological, vol. 41, no. 3, pp. 342–362, 2007.",
    "R. L. Burdett and E. Kozan, “Techniques for absolute capacity determination in railways,” Transportation Research Part B: Methodological, vol. 40, no. 8, pp. 616–632, 2006.",
    "D. Šemrov, R. Marsetič, M. Žura, L. Todorovski, and A. Srdic, “Reinforcement learning approach for train rescheduling on a single-track railway,” Transportation Research Part B: Methodological, vol. 86, pp. 250–267, 2016.",
    "F. Corman and L. Meng, “A review of online dynamic models and algorithms for railway traffic management,” IEEE Transactions on Intelligent Transportation Systems, vol. 16, no. 3, pp. 1274–1284, 2015.",
    "L. Perron and F. Didier, “CP-SAT Solver,” Google OR-Tools. [Online]. Available: https://developers.google.com/optimization/cp/cp_solver",
    "G. Ke, Q. Meng, T. Finley, T. Wang, W. Chen, W. Ma, Q. Ye, and T.-Y. Liu, “LightGBM: A highly efficient gradient boosting decision tree,” in Proc. Advances in Neural Information Processing Systems (NIPS), 2017, pp. 3146–3154.",
    "East Coast Railway, Waltair Division, “Block section, station line and goods train movement records for the Kottavalasa–Palasa section, August 2025,” Indian Railways operational data release, 2025.",
]
for i, r in enumerate(refs, 1):
    p = doc.add_paragraph()
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.left_indent = Cm(1.0)
    p.paragraph_format.first_line_indent = Cm(-1.0)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.15
    run = p.add_run("[{}]\t{}".format(i, r))
    run.font.name = "Times New Roman"
    run.font.size = Pt(12)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")

doc.save(OUT)
print("written: {}".format(OUT))
