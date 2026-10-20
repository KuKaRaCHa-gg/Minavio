"""Interface graphique locale (Gradio) : `minavio ui`.

Quatre onglets : Montage, Captures de jeu, Réglages, Système. Toute la logique est dans
`minavio.ui.services` ; ce module ne fait que la mise en page et le câblage des événements.
Le serveur n'écoute que sur 127.0.0.1 : rien n'est exposé sur le réseau.
"""

from __future__ import annotations

from pathlib import Path

import gradio as gr

from minavio.errors import MinavioError
from minavio.ui import services as svc

REPO_ROOT = Path(__file__).resolve().parents[3]

INTRO = """# 🎮 Minavio
Montage automatique **Minivitch · Byte Tricks · The Lost Chat** — tout tourne sur ton PC."""


def build(cwd: Path, default_out: Path) -> gr.Blocks:
    profiles = svc.profile_choices()
    first = profiles[0][1] if profiles else None
    games = svc.game_choices()
    first_game = games[0][1] if games else None

    with gr.Blocks(title="Minavio") as demo:
        gr.Markdown(INTRO)
        proc_state = gr.State(None)

        # ------------------------------------------------------------------ Montage
        with gr.Tab("🎬 Montage"):
            with gr.Row():
                with gr.Column(scale=3):
                    profile = gr.Dropdown(profiles, value=first, label="Chaîne / profil")
                    inputs = gr.Textbox(
                        label="Dossier de clips",
                        placeholder=r"ex. C:\Users\toi\Videos\Outplayed\Apex Legends",
                        info="Dossier (sous-dossiers inclus) ou un seul fichier vidéo.",
                    )
                    clips_info = gr.Markdown()
                    music = gr.Textbox(
                        label="Musique (optionnel)",
                        placeholder="fichier .mp3 / .wav ; vide = music_dir du profil",
                    )
                    out_dir = gr.Textbox(value=str(default_out), label="Dossier de sortie")
                with gr.Column(scale=2):
                    formats = gr.CheckboxGroup(label="Formats")
                    shorts = gr.Slider(0, 10, step=1, label="Shorts par meilleur moment")
                    with gr.Row():
                        use_llm = gr.Checkbox(label="Qwen (titres, classement)")
                        chapters = gr.Checkbox(label="Chapitres YouTube")
                    with gr.Row():
                        resume = gr.Checkbox(label="Reprendre (réutilise work/)")
                        dry_run = gr.Checkbox(label="Test : s'arrêter au montage")
            with gr.Row():
                run_btn = gr.Button("▶ Lancer le montage", variant="primary", scale=3)
                stop_btn = gr.Button("■ Annuler", variant="stop", scale=1)
            with gr.Row():
                with gr.Column(scale=1):
                    progress = gr.Markdown("_Prêt._")
                with gr.Column(scale=2):
                    status = gr.Markdown()
                    videos = gr.Files(label="Vidéos produites", interactive=False)
                    preview = gr.Video(label="Aperçu", interactive=False)
                    summary = gr.Markdown()
                    chapters_txt = gr.Code(label="Chapitres (à coller dans la description)")
            with gr.Accordion("Journal détaillé", open=False):
                log = gr.Code(language=None, lines=14, max_lines=14, label=None)

            def on_profile(pid):
                d = svc.profile_defaults(pid)
                return (
                    gr.CheckboxGroup(choices=d["formats"], value=d["formats"]),
                    d["shorts"],
                    d["llm"],
                    d["chapters"],
                )

            profile.change(on_profile, profile, [formats, shorts, use_llm, chapters])
            demo.load(on_profile, profile, [formats, shorts, use_llm, chapters])
            inputs.blur(svc.count_clips, inputs, clips_info)

            def on_run(pid, folder, mus, fmts, n_shorts, llm, chap, res, dry, out):
                empty = (None, [], None, "", None)
                try:
                    args = svc.build_run_args(
                        pid,
                        folder,
                        music=mus,
                        formats=fmts,
                        all_formats=svc.profile_defaults(pid)["formats"],
                        shorts=n_shorts,
                        use_llm=llm,
                        chapters=chap,
                        resume=res,
                        dry_run=dry,
                        out_dir=out,
                    )
                except MinavioError as e:
                    yield None, "_Prêt._", f"❌ {e}", *empty[1:], ""
                    return
                state = None
                for state, proc in svc.run_job_process(args, cwd):
                    yield (
                        proc,
                        state.progress_markdown(),
                        "⏳ Montage en cours…",
                        gr.skip(),
                        gr.skip(),
                        gr.skip(),
                        gr.skip(),
                        "\n".join(state.log[-200:]),
                    )
                if state is None:
                    return
                if state.returncode == 0:
                    head = "✅ Terminé." if not dry else "✅ Montage planifié (timeline.json)."
                else:
                    head = f"❌ Échec : {state.error or 'voir le journal'}"
                if state.warnings:
                    head += "\n\n⚠️ " + "\n\n⚠️ ".join(dict.fromkeys(state.warnings))
                info = svc.job_summary(state.job_id, out, pid, cwd)
                yield (
                    None,
                    state.progress_markdown(),
                    head,
                    [str(p) for p in state.outputs] or None,
                    str(state.outputs[0]) if state.outputs else None,
                    svc.summary_markdown(info) if state.returncode == 0 else "",
                    info.get("chapters"),
                    "\n".join(state.log[-400:]),
                )

            run_btn.click(
                on_run,
                [
                    profile,
                    inputs,
                    music,
                    formats,
                    shorts,
                    use_llm,
                    chapters,
                    resume,
                    dry_run,
                    out_dir,
                ],
                [proc_state, progress, status, videos, preview, summary, chapters_txt, log],
            )

            def on_stop(proc):
                svc.cancel(proc)
                return None, "⏹ Annulé."

            stop_btn.click(on_stop, proc_state, [proc_state, status])

            def on_pick_video(files, evt: gr.SelectData):
                return files[evt.index] if files else None

            videos.select(on_pick_video, videos, preview)

        # ------------------------------------------------------------------ Captures
        with gr.Tab("🎯 Captures de jeu"):
            gr.Markdown(
                "Pour reconnaître les **kills** et les bandeaux, Minavio compare l'écran à de "
                "petites captures de l'interface du jeu. Fais une capture d'écran en 1080p au "
                "bon moment, **recadre uniquement l'élément** avec l'outil de recadrage, puis "
                "enregistre."
            )
            with gr.Row():
                game = gr.Dropdown(games, value=first_game, label="Jeu")
                tpl = gr.Dropdown(label="Capture à fournir")
            tpl_table = gr.Markdown()
            hint = gr.Markdown()
            editor = gr.ImageEditor(
                type="pil",
                label="Capture d'écran (recadre l'élément)",
                transforms=("crop",),
                sources=("upload", "clipboard"),
                height=520,
            )
            with gr.Row():
                save_tpl = gr.Button("💾 Enregistrer cette capture", variant="primary")
                tpl_msg = gr.Markdown()
            current = gr.Image(label="Capture actuelle", interactive=False, height=160)

            def on_game(gid):
                names = [t.name for t in svc.template_statuses(gid)] if gid else []
                missing = (
                    [t.name for t in svc.template_statuses(gid) if not t.exists] if gid else []
                )
                pick = (missing or names or [None])[0]
                return gr.Dropdown(choices=names, value=pick), svc.templates_markdown(gid)

            def on_tpl(gid, name):
                if not gid or not name:
                    return "", None
                st = next(t for t in svc.template_statuses(gid) if t.name == name)
                return svc.roi_hint(gid, name), str(st.path) if st.exists else None

            def on_save(gid, name, value):
                image = (value or {}).get("composite") if isinstance(value, dict) else value
                msg = svc.save_template(gid, name, image)
                _, cur = on_tpl(gid, name)
                return msg, svc.templates_markdown(gid), cur

            game.change(on_game, game, [tpl, tpl_table])
            demo.load(on_game, game, [tpl, tpl_table])
            tpl.change(on_tpl, [game, tpl], [hint, current])
            save_tpl.click(on_save, [game, tpl, editor], [tpl_msg, tpl_table, current])

        # ------------------------------------------------------------------ Réglages
        with gr.Tab("⚙️ Réglages"):
            gr.Markdown(
                "Réglages complets d'une chaîne (seuils d'autocut, effets sonores, cadrage…). "
                "Chaque champ est expliqué dans `configs/README.md`. Le fichier est **vérifié "
                "avant d'être enregistré** et l'ancienne version est gardée en `.bak`."
            )
            cfg_profile = gr.Dropdown(profiles, value=first, label="Profil")
            cfg_text = gr.Code(language="yaml", lines=28, label="Profil (YAML)")
            with gr.Row():
                check_btn = gr.Button("✔ Vérifier")
                save_btn = gr.Button("💾 Enregistrer", variant="primary")
                reload_btn = gr.Button("↺ Recharger")
            cfg_msg = gr.Markdown()

            def on_cfg_load(pid):
                return svc.read_profile_yaml(pid), ""

            def on_check(text, pid):
                err = svc.validate_profile_yaml(text, pid)
                return f"❌ {err}" if err else "✅ Profil valide."

            cfg_profile.change(on_cfg_load, cfg_profile, [cfg_text, cfg_msg])
            demo.load(on_cfg_load, cfg_profile, [cfg_text, cfg_msg])
            reload_btn.click(on_cfg_load, cfg_profile, [cfg_text, cfg_msg])
            check_btn.click(on_check, [cfg_text, cfg_profile], cfg_msg)
            save_btn.click(svc.save_profile_yaml, [cfg_text, cfg_profile], cfg_msg)

        # ------------------------------------------------------------------ Système
        with gr.Tab("🩺 Système"):
            sys_md = gr.Markdown()
            refresh = gr.Button("↻ Revérifier")
            gr.Markdown("### LLM local (Qwen)")
            with gr.Row():
                llm_profile = gr.Dropdown(profiles, value=first, label="Profil")
                ping_btn = gr.Button("Tester la connexion")
            ping_md = gr.Markdown()

            def on_sys():
                return svc.system_markdown(REPO_ROOT)

            demo.load(on_sys, None, sys_md)
            refresh.click(on_sys, None, sys_md)
            ping_btn.click(svc.llm_ping_markdown, llm_profile, ping_md)

    return demo


def launch(port: int = 7860, open_browser: bool = True, out_dir: Path | None = None) -> None:
    cwd = Path.cwd()
    out = (out_dir or cwd / "out").expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    demo = build(cwd, out)
    demo.queue(default_concurrency_limit=1)  # un montage à la fois (GPU)
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        inbrowser=open_browser,
        allowed_paths=[str(out), str(REPO_ROOT / "configs")],
        show_error=True,
    )
