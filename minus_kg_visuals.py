from __future__ import annotations

from io import BytesIO

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Bot keeps working without the optional picture.
    Image = None
    ImageDraw = None
    ImageFont = None


def _font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_fasting_ring(
    *,
    mode_label: str,
    phase: str,
    remaining_text: str,
    progress: float,
    eating: bool,
    language: str = "ru",
) -> bytes | None:
    if Image is None or ImageDraw is None:
        return None

    size = 900
    image = Image.new("RGB", (size, size), "#FFF9F2")
    draw = ImageDraw.Draw(image)

    center = size // 2
    margin = 105
    box = (margin, margin, size - margin, size - margin)
    width = 76

    draw.arc(box, start=-90, end=270, fill="#E4DED7", width=width)
    accent = "#34A853" if eating else "#5B5BD6"
    end_angle = -90 + 360 * min(1.0, max(0.0, progress))
    draw.arc(box, start=-90, end=end_angle, fill=accent, width=width)

    inner = 225
    draw.ellipse(
        (inner, inner, size - inner, size - inner),
        fill="#FFFFFF",
        outline="#EFE7DF",
        width=4,
    )

    mode_font = _font(88, bold=True)
    phase_font = _font(58, bold=True)
    time_font = _font(94, bold=True)
    small_font = _font(34)

    def centered(text: str, y: int, font, fill: str) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = center - (bbox[2] - bbox[0]) / 2
        draw.text((x, y), text, font=font, fill=fill)

    centered(mode_label, 292, mode_font, "#27252A")
    centered(phase, 410, phase_font, accent)
    centered(remaining_text, 505, time_font, "#27252A")
    remaining_label = "ДО ЗМІНИ" if language == "uk" else "ДО СМЕНЫ"
    centered(remaining_label, 635, small_font, "#817B75")

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()



def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_recipe_card(
    *,
    recipe: dict,
    index: int,
    total: int,
    language: str,
) -> bytes | None:
    """Create a readable vertical recipe infographic for Telegram."""
    if Image is None or ImageDraw is None:
        return None

    width, height = 1080, 1350
    image = Image.new("RGB", (width, height), "#FFF8F0")
    draw = ImageDraw.Draw(image)

    # Header
    draw.rounded_rectangle(
        (45, 40, width - 45, 270),
        radius=44,
        fill="#FFFFFF",
        outline="#EEE2D7",
        width=4,
    )
    draw.rounded_rectangle(
        (70, 70, 280, 125),
        radius=26,
        fill="#5B5BD6",
    )

    label_font = _font(28, bold=True)
    title_font = _font(52, bold=True)
    subtitle_font = _font(30)
    section_font = _font(34, bold=True)
    body_font = _font(29)
    small_font = _font(25)
    macro_font = _font(23, bold=True)

    meal_type = str(recipe.get("meal_type") or "").upper()
    draw.text((92, 82), meal_type[:18], font=label_font, fill="#FFFFFF")

    title = str(recipe.get("title") or "Рецепт")
    title_lines = _wrap_text(draw, title, title_font, 890)[:2]
    y = 145
    for line in title_lines:
        draw.text((75, y), line, font=title_font, fill="#242228")
        y += 61

    portion = str(recipe.get("portion") or "1 порция")
    draw.text((75, 235), portion[:70], font=subtitle_font, fill="#746F69")

    # Nutrition chips. Full words are clearer than unexplained
    # abbreviations, so the four values are arranged in two rows.
    if language == "uk":
        macros = [
            (f"Енергія {recipe.get('calories', 0)} ккал", "#F1E8FF"),
            (f"Білок {recipe.get('protein', 0)} г", "#E6F6EB"),
            (f"Жири {recipe.get('fat', 0)} г", "#FFF0D6"),
            (f"Вуглеводи {recipe.get('carbs', 0)} г", "#E8F2FF"),
        ]
    else:
        macros = [
            (f"Энергия {recipe.get('calories', 0)} ккал", "#F1E8FF"),
            (f"Белок {recipe.get('protein', 0)} г", "#E6F6EB"),
            (f"Жиры {recipe.get('fat', 0)} г", "#FFF0D6"),
            (f"Углеводы {recipe.get('carbs', 0)} г", "#E8F2FF"),
        ]

    chip_positions = [
        (55, 300, 520, 358),
        (545, 300, 1010, 358),
        (55, 372, 520, 430),
        (545, 372, 1010, 430),
    ]
    for (text, fill), box in zip(macros, chip_positions):
        draw.rounded_rectangle(
            box,
            radius=26,
            fill=fill,
        )
        bbox = draw.textbbox((0, 0), text, font=macro_font)
        text_width = bbox[2] - bbox[0]
        text_x = box[0] + (box[2] - box[0] - text_width) / 2
        draw.text(
            (text_x, box[1] + 15),
            text,
            font=macro_font,
            fill="#39353C",
        )

    # Two content panels
    left = (45, 465, 515, 1180)
    right = (545, 465, 1035, 1180)
    for box in (left, right):
        draw.rounded_rectangle(
            box,
            radius=36,
            fill="#FFFFFF",
            outline="#EEE2D7",
            width=3,
        )

    ingredients_title = "ІНГРЕДІЄНТИ" if language == "uk" else "ИНГРЕДИЕНТЫ"
    steps_title = "ПРИГОТОВЛЕНИЕ" if language == "ru" else "ПРИГОТУВАННЯ"
    draw.text((75, 500), ingredients_title, font=section_font, fill="#5B5BD6")
    draw.text((575, 500), steps_title, font=section_font, fill="#34A853")

    # Ingredients
    y = 560
    for ingredient in (recipe.get("ingredients") or [])[:8]:
        lines = _wrap_text(draw, str(ingredient), body_font, 380)[:2]
        draw.ellipse((78, y + 10, 94, y + 26), fill="#5B5BD6")
        line_y = y
        for line in lines:
            draw.text((110, line_y), line, font=body_font, fill="#333037")
            line_y += 38
        y = line_y + 18
        if y > 1110:
            break

    # Steps with arrows
    y = 560
    for step_index, step in enumerate((recipe.get("steps") or [])[:5], start=1):
        draw.rounded_rectangle(
            (575, y, 628, y + 53),
            radius=20,
            fill="#34A853",
        )
        number = str(step_index)
        num_bbox = draw.textbbox((0, 0), number, font=macro_font)
        num_x = 601 - (num_bbox[2] - num_bbox[0]) / 2
        draw.text((num_x, y + 13), number, font=macro_font, fill="#FFFFFF")

        lines = _wrap_text(draw, str(step), body_font, 340)[:3]
        line_y = y
        for line in lines:
            draw.text((650, line_y), line, font=body_font, fill="#333037")
            line_y += 38

        y = max(y + 72, line_y + 18)
        if step_index < min(5, len(recipe.get("steps") or [])):
            draw.line((602, y - 13, 602, y + 7), fill="#34A853", width=6)
            draw.polygon(
                [(592, y + 2), (612, y + 2), (602, y + 18)],
                fill="#34A853",
            )
            y += 25
        if y > 1110:
            break

    # Tip/footer
    tip = str(recipe.get("tip") or "").strip()
    if tip:
        draw.rounded_rectangle(
            (45, 1205, 1035, 1290),
            radius=30,
            fill="#FFF0D6",
        )
        tip_label = "Порада" if language == "uk" else "Совет"
        tip_lines = _wrap_text(
            draw,
            f"{tip_label}: {tip}",
            small_font,
            920,
        )[:2]
        tip_y = 1222
        for line in tip_lines:
            draw.text((75, tip_y), line, font=small_font, fill="#4B4135")
            tip_y += 31

    counter = f"{index + 1}/{total}"
    draw.text((930, 1310), counter, font=small_font, fill="#8B847D")

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()



def render_weight_progress_chart(
    *,
    points: list[dict],
    target_kg: float | None,
    language: str,
    start_weight_kg: float | None = None,
) -> bytes | None:
    """Render a clean Telegram-friendly weight progress chart."""
    if Image is None or ImageDraw is None or not points:
        return None

    width, height = 1200, 760
    image = Image.new("RGB", (width, height), "#FFF8F0")
    draw = ImageDraw.Draw(image)

    title_font = _font(44, bold=True)
    body_font = _font(27)
    small_font = _font(22)
    value_font = _font(30, bold=True)

    title = "Динаміка ваги" if language == "uk" else "Динамика веса"
    draw.text((60, 42), title, font=title_font, fill="#28252B")

    plot_left, plot_top = 100, 145
    plot_right, plot_bottom = 1135, 625
    draw.rounded_rectangle(
        (45, 115, 1155, 680),
        radius=34,
        fill="#FFFFFF",
        outline="#E9DED3",
        width=3,
    )

    values = [float(point["weight_kg"]) for point in points]
    if start_weight_kg is not None:
        values.append(float(start_weight_kg))
    if target_kg is not None:
        values.append(float(target_kg))
    minimum = min(values)
    maximum = max(values)
    padding = max(1.0, (maximum - minimum) * 0.20)
    y_min = max(0.0, minimum - padding)
    y_max = maximum + padding
    if y_max - y_min < 2:
        y_min -= 1
        y_max += 1

    # Horizontal grid and labels.
    for index in range(5):
        ratio = index / 4
        y = plot_bottom - ratio * (plot_bottom - plot_top)
        value = y_min + ratio * (y_max - y_min)
        draw.line(
            (plot_left, y, plot_right, y),
            fill="#EDE6DF",
            width=2,
        )
        label = f"{value:.1f}"
        bbox = draw.textbbox((0, 0), label, font=small_font)
        draw.text(
            (plot_left - (bbox[2] - bbox[0]) - 15, y - 12),
            label,
            font=small_font,
            fill="#7B746E",
        )

    count = len(points)
    if count == 1:
        x_positions = [(plot_left + plot_right) / 2]
    else:
        x_positions = [
            plot_left + index * (plot_right - plot_left) / (count - 1)
            for index in range(count)
        ]

    def y_position(value: float) -> float:
        return plot_bottom - (
            (value - y_min) / (y_max - y_min)
        ) * (plot_bottom - plot_top)

    if target_kg is not None and y_min <= target_kg <= y_max:
        target_y = y_position(float(target_kg))
        # Dashed target line.
        segment = 26
        x = plot_left
        while x < plot_right:
            draw.line(
                (x, target_y, min(x + segment, plot_right), target_y),
                fill="#34A853",
                width=4,
            )
            x += segment * 2
        target_label = (
            f"Ціль {target_kg:.1f} кг"
            if language == "uk"
            else f"Цель {target_kg:.1f} кг"
        )
        draw.text(
            (plot_right - 180, target_y - 35),
            target_label,
            font=small_font,
            fill="#25833F",
        )

    line_points = [
        (x, y_position(float(point["weight_kg"])))
        for x, point in zip(x_positions, points)
    ]
    if len(line_points) > 1:
        draw.line(line_points, fill="#5B5BD6", width=8, joint="curve")

    for x, y in line_points:
        draw.ellipse(
            (x - 11, y - 11, x + 11, y + 11),
            fill="#FFFFFF",
            outline="#5B5BD6",
            width=6,
        )

    # Date labels: first, middle and last.
    label_indexes = sorted(set([0, count // 2, count - 1]))
    for index in label_indexes:
        point = points[index]
        label = str(point["local_date"])[5:].replace("-", ".")
        bbox = draw.textbbox((0, 0), label, font=small_font)
        draw.text(
            (
                x_positions[index] - (bbox[2] - bbox[0]) / 2,
                plot_bottom + 18,
            ),
            label,
            font=small_font,
            fill="#7B746E",
        )

    start = (
        float(start_weight_kg)
        if start_weight_kg is not None
        else float(points[0]["weight_kg"])
    )
    current = float(points[-1]["weight_kg"])
    change = current - start
    change_text = f"{change:+.1f} кг"
    change_fill = "#25833F" if change < 0 else (
        "#C43D4B" if change > 0 else "#5B5BD6"
    )

    start_label = "Старт" if language == "ru" else "Старт"
    current_label = "Зараз" if language == "uk" else "Сейчас"
    draw.text(
        (65, 700),
        f"{start_label}: {start:.1f} кг",
        font=body_font,
        fill="#4B474D",
    )
    draw.text(
        (410, 700),
        f"{current_label}: {current:.1f} кг",
        font=body_font,
        fill="#4B474D",
    )
    draw.text(
        (850, 694),
        change_text,
        font=value_font,
        fill=change_fill,
    )

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()



def _save_gif(
    frames: list,
    duration: int = 180,
    loop: int = 0,
) -> bytes | None:
    if not frames:
        return None
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=loop,
        optimize=True,
        disposal=2,
    )
    return buffer.getvalue()


def render_welcome_animation() -> bytes | None:
    if Image is None or ImageDraw is None:
        return None

    frames = []
    size = 520
    title_font = _font(62, bold=True)
    subtitle_font = _font(30)
    heart_font = _font(66, bold=True)

    for index in range(18):
        phase = index / 18
        pulse = 1.0 - abs(phase * 2 - 1)
        radius = int(118 + 24 * pulse)
        image = Image.new("RGB", (size, size), "#FFF7EF")
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (size // 2 - radius, 150 - radius, size // 2 + radius, 150 + radius),
            fill="#E8E7FF",
            outline="#5B5BD6",
            width=7,
        )
        heart = "♥"
        bbox = draw.textbbox((0, 0), heart, font=heart_font)
        draw.text(
            (size // 2 - (bbox[2] - bbox[0]) / 2,
             150 - (bbox[3] - bbox[1]) / 2 - 10),
            heart,
            font=heart_font,
            fill="#5B5BD6",
        )
        title = "minus_kg"
        bbox = draw.textbbox((0, 0), title, font=title_font)
        draw.text(
            (size // 2 - (bbox[2] - bbox[0]) / 2, 300),
            title,
            font=title_font,
            fill="#29262D",
        )
        subtitle = "крок за кроком · шаг за шагом"
        bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
        draw.text(
            (size // 2 - (bbox[2] - bbox[0]) / 2, 390),
            subtitle,
            font=subtitle_font,
            fill="#6E6863",
        )
        frames.append(image)

    return _save_gif(frames, duration=130)


def render_breathing_animation(language: str) -> bytes | None:
    """
    One-minute breathing guide with a visible countdown.

    Rhythm:
    - inhale for 4 seconds;
    - exhale for 6 seconds;
    - six cycles total = 60 seconds.
    """
    if Image is None or ImageDraw is None:
        return None

    frames = []
    size = 560
    title_font = _font(38, bold=True)
    phase_font = _font(52, bold=True)
    timer_font = _font(38, bold=True)
    small_font = _font(27)

    total_seconds = 60
    inhale_seconds = 4
    exhale_seconds = 6
    cycle_seconds = inhale_seconds + exhale_seconds

    for elapsed in range(total_seconds):
        remaining = total_seconds - elapsed

        cycle_position = elapsed % cycle_seconds
        inhaling = cycle_position < inhale_seconds

        if inhaling:
            phase_progress = cycle_position / max(1, inhale_seconds - 1)
            radius = int(95 + 105 * phase_progress)
        else:
            exhale_position = cycle_position - inhale_seconds
            phase_progress = exhale_position / max(1, exhale_seconds - 1)
            radius = int(200 - 105 * phase_progress)

        image = Image.new("RGB", (size, size), "#FFF8F0")
        draw = ImageDraw.Draw(image)

        draw.rounded_rectangle(
            (24, 24, size - 24, size - 24),
            radius=42,
            fill="#FFFFFF",
            outline="#E9DED3",
            width=4,
        )

        accent = "#5B5BD6" if inhaling else "#34A853"
        circle_fill = "#F0EEFF" if inhaling else "#E9F7ED"

        draw.ellipse(
            (
                size // 2 - radius,
                245 - radius,
                size // 2 + radius,
                245 + radius,
            ),
            fill=circle_fill,
            outline=accent,
            width=9,
        )

        title = "Хвилина спокою" if language == "uk" else "Минута спокойствия"
        bbox = draw.textbbox((0, 0), title, font=title_font)
        draw.text(
            (size // 2 - (bbox[2] - bbox[0]) / 2, 55),
            title,
            font=title_font,
            fill="#29262D",
        )

        phase_text = (
            ("ВДИХ" if inhaling else "ВИДИХ")
            if language == "uk"
            else ("ВДОХ" if inhaling else "ВЫДОХ")
        )
        bbox = draw.textbbox((0, 0), phase_text, font=phase_font)
        draw.text(
            (
                size // 2 - (bbox[2] - bbox[0]) / 2,
                220 - (bbox[3] - bbox[1]) / 2,
            ),
            phase_text,
            font=phase_font,
            fill=accent,
        )

        timer_text = f"00:{remaining:02d}"
        bbox = draw.textbbox((0, 0), timer_text, font=timer_font)
        draw.text(
            (
                size // 2 - (bbox[2] - bbox[0]) / 2,
                315,
            ),
            timer_text,
            font=timer_font,
            fill="#2F2B33",
        )

        if language == "uk":
            hint = (
                "Вдихайте носом"
                if inhaling
                else "Повільно видихайте"
            )
        else:
            hint = (
                "Вдыхайте носом"
                if inhaling
                else "Медленно выдыхайте"
            )

        bbox = draw.textbbox((0, 0), hint, font=small_font)
        draw.text(
            (
                size // 2 - (bbox[2] - bbox[0]) / 2,
                455,
            ),
            hint,
            font=small_font,
            fill="#706A65",
        )

        frames.append(image)

    # Final two-second frame.
    final = Image.new("RGB", (size, size), "#FFF8F0")
    draw = ImageDraw.Draw(final)
    draw.rounded_rectangle(
        (24, 24, size - 24, size - 24),
        radius=42,
        fill="#FFFFFF",
        outline="#E9DED3",
        width=4,
    )

    done_title = "Готово" if language == "uk" else "Готово"
    done_text = (
        "Зверніть увагу, чи стало хоча б трохи спокійніше."
        if language == "uk"
        else "Заметьте, стало ли хотя бы немного спокойнее."
    )

    bbox = draw.textbbox((0, 0), done_title, font=phase_font)
    draw.text(
        (
            size // 2 - (bbox[2] - bbox[0]) / 2,
            190,
        ),
        done_title,
        font=phase_font,
        fill="#34A853",
    )

    lines = _wrap_text(draw, done_text, small_font, 430)
    y = 315
    for line in lines[:3]:
        bbox = draw.textbbox((0, 0), line, font=small_font)
        draw.text(
            (
                size // 2 - (bbox[2] - bbox[0]) / 2,
                y,
            ),
            line,
            font=small_font,
            fill="#5F5954",
        )
        y += 38

    frames.extend([final, final])

    # loop=1: Telegram may still offer manual replay, but it no longer loops forever.
    return _save_gif(
        frames,
        duration=1000,
        loop=1,
    )




def _centered_text(draw, text: str, y: int, font, fill: str, width: int) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (width - (bbox[2] - bbox[0])) / 2
    draw.text((x, y), text, font=font, fill=fill)


def render_recipe_picker_animation(language: str) -> bytes | None:
    """Animated plate assembly shown while the gallery is being prepared."""
    if Image is None or ImageDraw is None:
        return None

    width, height = 560, 560
    frames = []
    title_font = _font(38, bold=True)
    phase_font = _font(29, bold=True)
    small_font = _font(24)

    ingredients = [
        ("#34A853", -170, 90),
        ("#F4B400", 720, 130),
        ("#5B5BD6", -120, 205),
        ("#EA6A47", 680, 250),
        ("#73B86B", -180, 310),
    ]

    for frame_index in range(30):
        image = Image.new("RGB", (width, height), "#FFF8F0")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (20, 20, width - 20, height - 20),
            radius=42,
            fill="#FFFFFF",
            outline="#E9DED3",
            width=4,
        )

        title = (
            "Збираю смачний варіант"
            if language == "uk"
            else "Собираю вкусный вариант"
        )
        _centered_text(draw, title, 48, title_font, "#29262D", width)

        # Plate
        draw.ellipse(
            (125, 180, 435, 490),
            fill="#F7F2EC",
            outline="#D9D1C9",
            width=8,
        )
        draw.ellipse(
            (170, 225, 390, 445),
            fill="#FFFFFF",
            outline="#E8E0D8",
            width=5,
        )

        progress = min(1.0, frame_index / 21)
        for number, (color, start_x, target_y) in enumerate(ingredients):
            local = max(0.0, min(1.0, progress * 1.35 - number * 0.13))
            target_x = 230 + (number % 3) * 50
            x = start_x + (target_x - start_x) * local
            y = target_y + (315 - target_y) * local
            radius = 25 if number != 2 else 31
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=color,
                outline="#FFFFFF",
                width=4,
            )

        # Sparkle / pulse
        pulse = 7 + int(7 * (1 - abs((frame_index % 10) / 5 - 1)))
        draw.ellipse(
            (447 - pulse, 116 - pulse, 447 + pulse, 116 + pulse),
            fill="#F4B400",
        )
        draw.ellipse(
            (105 - pulse // 2, 145 - pulse // 2,
             105 + pulse // 2, 145 + pulse // 2),
            fill="#5B5BD6",
        )

        phase_index = min(2, frame_index // 10)
        if language == "uk":
            phases = [
                "Дивлюся записи за сьогодні…",
                "Підбираю баланс і порцію…",
                "Залишилося обрати смак…",
            ]
            hint = "Смачно, зрозуміло й без сумного листка салату"
        else:
            phases = [
                "Смотрю записи за сегодня…",
                "Подбираю баланс и порцию…",
                "Осталось выбрать вкус…",
            ]
            hint = "Вкусно, понятно и без грустного листа салата"

        _centered_text(
            draw,
            phases[phase_index],
            495,
            phase_font,
            "#5B5BD6",
            width,
        )
        _centered_text(draw, hint, 530, small_font, "#746F69", width)
        frames.append(image)

    return _save_gif(frames, duration=115, loop=0)


def render_recipe_choice_animation(
    recipe: dict,
    language: str,
) -> bytes | None:
    """Short celebratory animation after the user chooses a recipe."""
    if Image is None or ImageDraw is None:
        return None

    width, height = 560, 560
    frames = []
    title_font = _font(41, bold=True)
    dish_font = _font(31, bold=True)
    small_font = _font(25)

    title = (
        "Вибір збережено"
        if language == "uk"
        else "Выбор сохранён"
    )
    subtitle = (
        "Це поки план, а не запис у щоденнику"
        if language == "uk"
        else "Это пока план, а не запись в дневнике"
    )
    dish_title = str(recipe.get("title") or "Рецепт")[:52]
    dish_lines = []

    for frame_index in range(24):
        image = Image.new("RGB", (width, height), "#FFF8F0")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (20, 20, width - 20, height - 20),
            radius=42,
            fill="#FFFFFF",
            outline="#E9DED3",
            width=4,
        )

        pulse = 1.0 - abs((frame_index % 12) / 6 - 1)
        plate_radius = int(132 + pulse * 8)

        draw.ellipse(
            (
                width // 2 - plate_radius,
                265 - plate_radius,
                width // 2 + plate_radius,
                265 + plate_radius,
            ),
            fill="#F7F2EC",
            outline="#34A853",
            width=8,
        )
        draw.ellipse(
            (195, 180, 365, 350),
            fill="#FFFFFF",
            outline="#E7DFD7",
            width=5,
        )

        # Food shapes
        draw.arc((215, 205, 345, 325), 15, 340, fill="#F4B400", width=26)
        draw.ellipse((242, 225, 285, 268), fill="#73B86B")
        draw.ellipse((292, 258, 332, 298), fill="#EA6A47")
        draw.rounded_rectangle(
            (225, 282, 285, 320),
            radius=13,
            fill="#5B5BD6",
        )

        # Steam moves upward.
        steam_shift = frame_index % 10
        for offset in (-35, 0, 35):
            x = width // 2 + offset
            y = 162 - steam_shift * 2
            draw.arc(
                (x - 18, y - 45, x + 18, y + 30),
                70,
                270,
                fill="#B6AEA7",
                width=5,
            )

        _centered_text(draw, title, 48, title_font, "#29262D", width)

        if not dish_lines:
            dish_lines = _wrap_text(
                draw,
                dish_title,
                dish_font,
                460,
            )[:2]
        y = 405
        for line in dish_lines:
            _centered_text(draw, line, y, dish_font, "#5B5BD6", width)
            y += 39

        _centered_text(draw, subtitle, 505, small_font, "#746F69", width)
        frames.append(image)

    return _save_gif(frames, duration=125, loop=1)
