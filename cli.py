"""Command-line driver for the pipeline (the Streamlit app calls the same core functions).

  python cli.py new --sample led_sneaker          create a project from a sample product
  python cli.py storyboard <id>                   M3 drafts + checks the storyboard
  python cli.py show <id>                         print storyboard and shot states
  (edit data/projects/<id>/storyboard.json by hand if you want, then)
  python cli.py approve <id> [--resolution 768P]  user confirmation: locks the storyboard
  python cli.py run <id>                          generate / resume all shots (H3 + M3 QC)
  python cli.py retry <id> <shot> [--visual ...]  regenerate a single shot (optionally with --camera / --sound)
  python cli.py assemble <id>                     join shots into final.mp4
  python cli.py demo --sample led_sneaker         all of the above in one go (auto-approves)
  python cli.py list
"""
import argparse
import json
import sys

from core import assemble, config, pipeline, storyboard
from core.project import Project, State, Storyboard


def format_storyboard(sb: Storyboard) -> str:
    lines = [f"外观: {sb.appearance}", f"款式: {sb.variants or '（只有一款）'}", f"音乐: {sb.music_style}", ""]
    for i, s in enumerate(sb.shots, 1):
        lines += [
            f"镜头{i}  {s.purpose}  ({s.selling_point or '-'})",
            f"  字幕: {s.caption}",
            f"  画面: {s.visual}",
            f"  运镜: {s.camera}",
            f"  音效: {s.sound}",
        ]
    if sb.qc:
        verdict = "通过" if sb.qc.get("pass") else "未通过"
        lines += ["", f"分镜QC: {verdict} {json.dumps(sb.qc.get('issues', []), ensure_ascii=False)}"]
    lines.append(f"已确认: {'是' if sb.approved else '否'}")
    return "\n".join(lines)


def format_state(st: State) -> str:
    lines = [f"resolution {st.resolution}"]
    for i, s in enumerate(st.shots, 1):
        line = f"shot {i}: {s.status:<10} attempts={s.attempt}"
        if s.qc:
            line += f" fidelity={s.qc.get('product_fidelity')} quality={s.qc.get('visual_quality')}"
        if s.error:
            line += f" error={s.error}"
        lines.append(line)
    if st.final:
        lines.append(f"final: {st.final}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("new").add_argument("--sample", required=True)
    for name in ("storyboard", "show", "run", "assemble"):
        sub.add_parser(name).add_argument("id")
    approve = sub.add_parser("approve")
    approve.add_argument("id")
    approve.add_argument("--resolution", choices=config.RESOLUTIONS, default=config.RESOLUTIONS[0])
    retry = sub.add_parser("retry")
    retry.add_argument("id")
    retry.add_argument("shot", type=int, choices=range(1, config.SHOT_COUNT + 1))
    retry.add_argument("--visual", help="new 画面 for the shot")
    retry.add_argument("--camera", help="new 运镜 for the shot")
    retry.add_argument("--sound", help="new 音效 for the shot")
    demo = sub.add_parser("demo")
    demo.add_argument("--sample", required=True)
    demo.add_argument("--resolution", choices=config.RESOLUTIONS, default=config.RESOLUTIONS[0])
    return ap.parse_args()


def main():
    args = parse_args()
    if args.cmd == "list":
        print("\n".join(Project.list_ids()) or "(no projects)")
    elif args.cmd == "new":
        print(Project.from_sample(args.sample).id)
    elif args.cmd == "demo":
        proj = Project.from_sample(args.sample)
        print(f"project {proj.id}")
        print(format_storyboard(storyboard.generate(proj)))
        pipeline.approve(proj, args.resolution)
        if pipeline.run(proj).all_done:
            assemble.assemble(proj)
        print(format_state(proj.load_active_state()))
    else:
        proj = Project(args.id)
        if args.cmd == "storyboard":
            print(format_storyboard(storyboard.generate(proj)))
        elif args.cmd == "show":
            sb, st = proj.load_storyboard(), proj.load_active_state()
            print(format_storyboard(sb) if sb else "(no storyboard)", end="\n\n")
            print(format_state(st) if st else "(not approved yet)")
        elif args.cmd == "approve":
            pipeline.approve(proj, args.resolution)
            print("approved")
        elif args.cmd == "run":
            print(format_state(pipeline.run(proj)))
        elif args.cmd == "retry":
            st = pipeline.retry_shot(proj, args.shot - 1, visual=args.visual, camera=args.camera, sound=args.sound)
            print(format_state(st))
        elif args.cmd == "assemble":
            assemble.assemble(proj)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, FileNotFoundError) as e:
        sys.exit(f"error: {e}")
