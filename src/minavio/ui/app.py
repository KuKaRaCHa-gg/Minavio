"""Interface graphique locale (Gradio) : `minavio ui`.

Six onglets : Montage, Miniatures, Captures de jeu, Réglages, Publication, Système. La logique est
dans `minavio.ui.services` et `minavio.ui.publish_services` ; ce module ne fait que la mise en page
et le câblage des événements.
Le serveur n'écoute que sur 127.0.0.1 : rien n'est exposé sur le réseau.
"""

from __future__ import annotations

from pathlib import Path

import gradio as gr

from minavio.errors import MinavioError
from minavio.ui import publish_services as psvc
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
                        thumbs = gr.Checkbox(label="Miniatures")
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
                    thumbs_gallery = gr.Gallery(
                        label="Miniatures produites", columns=3, height=260, interactive=False
                    )
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
                    d["thumbnails"],
                )

            profile.change(on_profile, profile, [formats, shorts, use_llm, chapters, thumbs])
            demo.load(on_profile, profile, [formats, shorts, use_llm, chapters, thumbs])
            inputs.blur(svc.count_clips, inputs, clips_info)

            def on_run(pid, folder, mus, fmts, n_shorts, llm, chap, thb, res, dry, out):
                empty = (None, [], None, [], "", None)
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
                        thumbnails=thb,
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
                    [str(p) for p in state.thumbs] or None,
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
                    thumbs,
                    resume,
                    dry_run,
                    out_dir,
                ],
                [
                    proc_state,
                    progress,
                    status,
                    videos,
                    preview,
                    thumbs_gallery,
                    summary,
                    chapters_txt,
                    log,
                ],
            )

            def on_stop(proc):
                svc.cancel(proc)
                return None, "⏹ Annulé."

            stop_btn.click(on_stop, proc_state, [proc_state, status])

            def on_pick_video(files, evt: gr.SelectData):
                return files[evt.index] if files else None

            videos.select(on_pick_video, videos, preview)

        # ------------------------------------------------------------------ Miniatures
        with gr.Tab("🖼️ Miniatures"):
            gr.Markdown(
                "Régénère les miniatures d'un montage déjà fait (étapes réutilisées depuis "
                "`work/`). Laisse le texte vide pour que Minavio le propose."
            )
            with gr.Row():
                th_profile = gr.Dropdown(profiles, value=first, label="Profil")
                th_job = gr.Dropdown(
                    svc.job_choices(cwd / "work"), label="Job récent (work/, date de modification)"
                )
                th_refresh = gr.Button("↻ Actualiser la liste")
            with gr.Row():
                th_text = gr.Textbox(
                    label="Texte personnalisé (optionnel)", placeholder="ex. 3 KILLS EN 5 SECONDES"
                )
                th_layout = gr.Dropdown(svc.THUMB_LAYOUTS, value="auto", label="Disposition")
            th_out = gr.Textbox(value=str(default_out), label="Dossier de sortie")
            th_btn = gr.Button("🖼️ Générer", variant="primary")
            th_msg = gr.Markdown()
            th_gallery = gr.Gallery(label="Miniatures", columns=3, height=360, interactive=False)
            th_files = gr.Files(label="Télécharger", interactive=False)

            def on_th_refresh():
                return gr.Dropdown(choices=svc.job_choices(cwd / "work"))

            def on_th_run(pid, job_id, text, layout, out):
                try:
                    args = svc.build_thumbnails_args(
                        pid,
                        job_id,
                        text=text,
                        layout=layout,
                        out_dir=out,
                        work_dir=str(cwd / "work"),
                    )
                except MinavioError as e:
                    return f"❌ {e}", None, None
                state, files = svc.run_thumbnails_process(args, cwd)
                if state.returncode != 0 or not files:
                    return f"❌ Échec : {state.error or 'voir le journal'}", None, None
                msg = f"✅ {len(files)} miniature(s)."
                if state.warnings:
                    msg += "\n\n⚠️ " + "\n\n⚠️ ".join(dict.fromkeys(state.warnings))
                return msg, files, files

            th_refresh.click(on_th_refresh, None, th_job)
            demo.load(on_th_refresh, None, th_job)
            th_btn.click(
                on_th_run,
                [th_profile, th_job, th_text, th_layout, th_out],
                [th_msg, th_gallery, th_files],
            )

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

        # ------------------------------------------------------------------ Publication
        with gr.Tab("📤 Publication"):
            gr.Markdown(psvc.PUBLISH_WARNING)
            auth_md = gr.Markdown()
            with gr.Row():
                secret_file = gr.File(
                    label="client_secret.json (Google Cloud, une seule fois)",
                    file_types=[".json"],
                    type="filepath",
                )
                connect_btn = gr.Button("🔗 Connecter YouTube")
            with gr.Row():
                pb_profile = gr.Dropdown(profiles, value=first, label="Profil")
                pb_job = gr.Dropdown(
                    svc.job_choices(cwd / "work"), label="Job récent (work/, date de modification)"
                )
                pb_refresh = gr.Button("↻ Actualiser")
            with gr.Row():
                pb_video = gr.Dropdown(psvc.PUBLISH_VIDEOS, value="youtube", label="Vidéo")
                pb_privacy = gr.Dropdown(
                    ["private", "unlisted", "public"], value="private", label="Confidentialité"
                )
                pb_at = gr.Textbox(
                    label="Heure de mise en ligne (HH:MM locale)",
                    placeholder="vide = immédiat",
                    info="Programmer impose « privée » jusqu'à l'heure dite.",
                )
            pb_out = gr.Textbox(value=str(default_out), label="Dossier de sortie")
            pb_title = gr.Textbox(label="Titre (éditable)")
            pb_desc = gr.Textbox(label="Description + chapitres (éditable)", lines=8)
            pb_tags = gr.Textbox(label="Tags (séparés par des virgules)")
            pb_thumb = gr.Image(label="Miniature", interactive=False, height=200)
            with gr.Row():
                pb_preview_btn = gr.Button("🔍 Prévisualiser (dry-run)")
                pb_confirm = gr.Checkbox(label="Je confirme la publication sur YouTube")
                pb_publish_btn = gr.Button("📤 Publier", variant="primary")
            pb_msg = gr.Markdown()

            def on_pb_load(pid):
                s = psvc.profile_publish_settings(pid) if pid else {"privacy": "private"}
                return s["privacy"], s.get("schedule_at", "")

            def on_pb_refresh():
                return gr.Dropdown(choices=svc.job_choices(cwd / "work"))

            def on_pb_choices(pid, job, out):
                return gr.Dropdown(choices=psvc.video_choices(pid, job or "", out), value="youtube")

            def on_pb_preview(pid, job, video, privacy, at, out, title, desc, tags):
                try:
                    res = psvc.preview(
                        pid,
                        job,
                        video,
                        work_dir=cwd / "work",
                        out_dir=out,
                        privacy=privacy,
                        schedule_at=at or "now",
                        title=title,
                        description=desc,
                        tags=tags,
                    )
                except MinavioError as e:
                    return f"❌ {e}", gr.skip(), gr.skip(), gr.skip(), gr.skip()
                r = res[0]
                return (
                    "🔍 Prévisualisation (rien n'a été envoyé).\n\n" + psvc.results_markdown(res),
                    r["title"],
                    r["description"],
                    ", ".join(r["tags"]),
                    r["thumbnail"],
                )

            def on_pb_publish(pid, job, video, privacy, at, out, title, desc, tags, confirm):
                return psvc.run_publish(
                    pid,
                    job,
                    video,
                    work_dir=cwd / "work",
                    out_dir=out,
                    privacy=privacy,
                    schedule_at=at or "now",
                    title=title,
                    description=desc,
                    tags=tags,
                    dry_run=False,
                    confirm=confirm,
                )

            pb_inputs = [pb_profile, pb_job, pb_video, pb_privacy, pb_at, pb_out]
            pb_fields = [pb_title, pb_desc, pb_tags]
            pb_profile.change(on_pb_load, pb_profile, [pb_privacy, pb_at])
            demo.load(on_pb_load, pb_profile, [pb_privacy, pb_at])
            demo.load(on_pb_refresh, None, pb_job)
            demo.load(psvc.auth_markdown, None, auth_md)
            pb_refresh.click(on_pb_refresh, None, pb_job)
            pb_job.change(on_pb_choices, [pb_profile, pb_job, pb_out], pb_video)

            def on_pb_clear():  # textes d'un autre job : jamais envoyés par erreur
                return "", "", "", None

            for trigger in (pb_job, pb_profile, pb_video):
                trigger.change(on_pb_clear, None, [pb_title, pb_desc, pb_tags, pb_thumb])
            connect_btn.click(psvc.connect_youtube, secret_file, auth_md)
            pb_preview_btn.click(
                on_pb_preview,
                [*pb_inputs, *pb_fields],
                [pb_msg, pb_title, pb_desc, pb_tags, pb_thumb],
            )
            pb_publish_btn.click(on_pb_publish, [*pb_inputs, *pb_fields, pb_confirm], pb_msg).then(
                lambda: False, None, pb_confirm
            )

        # ------------------------------------------------------------------ Système
        with gr.Tab("🩺 Système"):
            sys_md = gr.Markdown()
            refresh = gr.Button("↻ Revérifier")
            gr.Markdown("### LLM local (Qwen)")
            with gr.Row():
                llm_profile = gr.Dropdown(profiles, value=first, label="Profil")
                ping_btn = gr.Button("Tester la connexion")
            ping_md = gr.Markdown()

            gr.Markdown("### LLM : fournisseurs et clés")
            gr.Markdown(svc.LLM_WARNING)
            provider_table = gr.Dataframe(
                headers=["Ordre", "Type", "Modèle", "Clé", "État"],
                interactive=False,
                label="Chaîne de fournisseurs du profil (essayés dans l'ordre)",
            )
            with gr.Row():
                prov_refresh = gr.Button("↻ Afficher")
                prov_ping = gr.Button("Tester chaque fournisseur")
            key_table = gr.Dataframe(
                headers=["Service", "Variable", "Source", "Aperçu"],
                interactive=False,
                label="Clés d'API (valeurs jamais affichées)",
            )
            gr.Markdown(
                "Jeton GitHub Models : crée un jeton *fine-grained* avec la permission "
                f"**Models: read** sur {svc.GITHUB_TOKEN_URL}"
            )
            with gr.Row():
                key_name = gr.Dropdown(
                    [(v, k) for k, v in svc.KEY_LABELS.items()],
                    value="ANTHROPIC_API_KEY",
                    label="Clé",
                )
                key_value = gr.Textbox(type="password", label="Valeur (masquée)")
            with gr.Row():
                key_save = gr.Button("💾 Enregistrer la clé", variant="primary")
                key_del = gr.Button("🗑 Supprimer la clé")
            key_msg = gr.Markdown()

            def on_providers(pid, ping=False):
                return svc.provider_rows(pid, ping=ping) if pid else []

            def on_providers_ping(pid):
                return on_providers(pid, True)

            def on_key_save(name, value):
                return svc.save_key(name, value), svc.key_status_rows(), ""

            def on_key_del(name):
                return svc.remove_key(name), svc.key_status_rows(), ""

            llm_profile.change(on_providers, llm_profile, provider_table)
            demo.load(on_providers, llm_profile, provider_table)
            demo.load(svc.key_status_rows, None, key_table)
            prov_refresh.click(on_providers, llm_profile, provider_table)
            prov_ping.click(on_providers_ping, llm_profile, provider_table)
            key_save.click(on_key_save, [key_name, key_value], [key_msg, key_table, key_value])
            key_del.click(on_key_del, key_name, [key_msg, key_table, key_value])

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
        allowed_paths=[str(out), str(cwd / "out"), str(cwd / "work"), str(REPO_ROOT / "configs")],
        show_error=True,
    )
