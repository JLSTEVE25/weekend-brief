"""Render functions for the "This Weekend" tab: timeline, weekend ideas, date night grid."""

import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def build_booking_link(platform, platform_url, date_str, time_slot=None, party_size=2):
    if not platform_url:
        return ''
    sep = '&' if '?' in platform_url else '?'
    if platform == 'Resy':
        return f'{platform_url}{sep}date={date_str}&seats={party_size}'
    elif platform == 'OpenTable':
        if time_slot:
            return f'{platform_url}{sep}dateTime={date_str}T{time_slot}&covers={party_size}'
        return f'{platform_url}{sep}dateTime={date_str}&covers={party_size}'
    elif platform == 'Tock':
        params = f'{sep}date={date_str}&size={party_size}'
        if time_slot:
            params += f'&time={time_slot}'
        return f'{platform_url}{params}'
    return platform_url


def render_weather_html(weather):
    if not weather:
        return '<div class="weather-day"><span class="weather-day-label">No data</span></div>'
    html_parts = []
    for day in weather:
        rain_pct = day.get("rain_pct", 0)
        rain_class = ' high-rain' if rain_pct >= 50 else ''
        html_parts.append(f'''      <div class="weather-day">
        <div class="weather-day-label">{day["day"]}</div>
        <div class="weather-icon">{day.get("icon", "🌤")}</div>
        <div class="weather-temps">{day["high"]}° <span class="weather-low">{day["low"]}°</span></div>
        <div class="weather-rain{rain_class}">💧 {rain_pct}%</div>
      </div>''')
    return "\n".join(html_parts)


def render_timeline_html(calendar_events, open_windows):
    if not calendar_events and not open_windows:
        return '    <div class="calendar-empty">Calendar sync coming soon</div>'
    days = {"FRIDAY": [], "SATURDAY": [], "SUNDAY": []}
    day_map = {"fri": "FRIDAY", "sat": "SATURDAY", "sun": "SUNDAY",
               "friday": "FRIDAY", "saturday": "SATURDAY", "sunday": "SUNDAY"}
    for ev in calendar_events:
        start = ev.get("start", "")
        if "T" in start:
            try:
                dt = datetime.datetime.fromisoformat(start)
                day_key = dt.strftime("%A").upper()
                sort_key = dt.strftime("%H:%M")
            except ValueError:
                continue
        else:
            try:
                dt = datetime.date.fromisoformat(start)
                day_key = dt.strftime("%A").upper()
                sort_key = "00:00"
            except ValueError:
                continue
        if day_key in days:
            ev["_sort_key"] = sort_key
            ev["_time_display"] = dt.strftime("%-I:%M %p").lstrip("0") if "T" in start else "All day"
            days[day_key].append(ev)
    for w in open_windows:
        day_key = day_map.get(w.get("day", "").lower(), "")
        if day_key in days:
            block = w.get("window", "evening")
            label = "Nothing planned ✨" if block != "morning" else "Morning free ✨"
            raw_time = w.get("start_time", "17:00")
            try:
                t = datetime.datetime.strptime(raw_time, "%H:%M")
                display_time = t.strftime("%-I:%M %p").lstrip("0")
            except ValueError:
                display_time = raw_time
            days[day_key].append({
                "_is_free": True, "_sort_key": raw_time,
                "_time_display": display_time, "label": label,
            })
    html_parts = ['    <div class="timeline-card">']
    for day_name in ["FRIDAY", "SATURDAY", "SUNDAY"]:
        events = days[day_name]
        if not events:
            html_parts.append(f'      <div class="timeline-day-header">{day_name}</div>')
            html_parts.append('      <div class="timeline-event free-window"><div class="timeline-connector"><div class="timeline-dot"></div></div><div class="timeline-body"><span class="free-text">All day free ✨</span></div></div>')
            continue
        events.sort(key=lambda e: e.get("_sort_key", "99:99"))
        html_parts.append(f'      <div class="timeline-day-header">{day_name}</div>')
        for ev in events:
            if ev.get("_is_free"):
                html_parts.append(f'''      <div class="timeline-event free-window">
        <div class="timeline-connector"><div class="timeline-dot"></div></div>
        <div class="timeline-time">{ev["_time_display"]}</div>
        <div class="timeline-body"><span class="free-text">{ev["label"]}</span></div>
      </div>''')
            else:
                cal = ev.get("calendar", "family")
                badge_class = "john" if "john" in cal.lower() else "sara" if "sara" in cal.lower() else "family"
                badge_letter = "J" if badge_class == "john" else "S" if badge_class == "sara" else "F"
                time_str = ev.get("_time_display", "All day")
                location_html = f'<div class="timeline-location">{ev["location"]}</div>' if ev.get("location") else ''
                html_parts.append(f'''      <div class="timeline-event has-event">
        <div class="timeline-connector"><div class="timeline-dot"></div></div>
        <div class="timeline-time">{time_str}</div>
        <div class="timeline-body">
          <div class="timeline-title">{ev.get("summary", "Event")}</div>
          {location_html}
        </div>
        <div class="cal-badge {badge_class}">{badge_letter}</div>
      </div>''')
    html_parts.append('    </div>')
    return "\n".join(html_parts)


def render_weekend_ideas_html(ideas):
    if not ideas:
        return ''
    html_parts = ['    <div class="section-label">Weekend Ideas</div>']
    for idea in ideas:
        safe_title = idea.get("title", "").replace("'", "&#39;")
        invite_html = f'<span class="chip chip-invite">👋 {idea["invite"]}</span>' if idea.get("invite") else ''
        window_day = idea.get("window_day", "saturday")
        chip_type = f"chip-{window_day}" if window_day in ("friday", "saturday", "sunday") else "chip-saturday"
        window_label = idea.get("window_label", "")

        html_parts.append(
            f'    <div class="idea-card" data-record-id="" data-name="{safe_title}" data-type="idea">\n'
            f'      <div class="idea-header">\n'
            f'        <span class="idea-emoji">{idea.get("emoji", "💡")}</span>\n'
            f'        <span class="idea-title">{idea.get("title", "")}</span>\n'
            f'      </div>\n'
            f'      <div class="idea-desc">{idea.get("description", "")}</div>\n'
            f'      <div class="chip-row"><span class="chip {chip_type}">{window_label}</span>{invite_html}</div>\n'
            f'      <div class="feedback-row">\n'
            f'        <button class="feedback-btn" onclick="handleFeedback(this,\'idea\',\'{safe_title}\',\'love\')"><span>❤️</span><span class="fb-label">Love</span></button>\n'
            f'        <button class="feedback-btn" onclick="handleFeedback(this,\'idea\',\'{safe_title}\',\'nope\')"><span>👎</span><span class="fb-label">Nope</span></button>\n'
            f'        <button class="feedback-btn" onclick="handleFeedback(this,\'idea\',\'{safe_title}\',\'interested\')"><span>👀</span><span class="fb-label">Interested</span></button>\n'
            f'        <button class="feedback-btn" onclick="handleFeedback(this,\'idea\',\'{safe_title}\',\'swap\')"><span>🔄</span><span class="fb-label">Swap</span></button>\n'
            f'      </div>\n'
            f'    </div>'
        )
    return "\n".join(html_parts)


def render_date_night_html(date_night, friday, saturday):
    if not date_night:
        return ''

    intro = date_night.get("intro_text", "Consider a date night:")
    target_day = date_night.get("target_day", "friday")
    restaurants = date_night.get("restaurants", [])
    if not restaurants:
        return ''

    target_date = friday if target_day == "friday" else saturday
    date_str = target_date.isoformat()

    html_parts = [
        '    <div class="section-label">Date Night</div>',
        f'    <div class="date-night-section">',
        f'      <div class="date-night-intro">{intro}</div>',
        f'      <div class="date-night-grid">',
    ]

    for r in restaurants:
        name = r.get("name", "")
        safe_name = name.replace("'", "&#39;")
        neighborhood = r.get("neighborhood", "")
        price = r.get("price", "")
        vibe = r.get("vibe", "")
        record_id = r.get("data_record_id", "")
        party_size = r.get("party_size", 2)

        slots_html = _render_dn_slots(r.get("_reservation"), target_day, date_str, party_size)

        html_parts.append(
            f'        <div class="dn-card" data-record-id="{record_id}" data-name="{name}" data-type="restaurant">\n'
            f'          <div class="dn-name">{name}</div>\n'
            f'          <div class="dn-vibe">{vibe}</div>\n'
            f'          <div class="dn-meta"><span class="dn-neighborhood">{neighborhood}</span> · <span class="dn-price">{price}</span></div>\n'
            f'{slots_html}'
            f'          <div class="feedback-row">\n'
            f'            <button class="feedback-btn" onclick="handleFeedback(this,\'restaurant\',\'{safe_name}\',\'love\')"><span>❤️</span><span class="fb-label">Love</span></button>\n'
            f'            <button class="feedback-btn" onclick="handleFeedback(this,\'restaurant\',\'{safe_name}\',\'nope\')"><span>👎</span><span class="fb-label">Nope</span></button>\n'
            f'            <button class="feedback-btn" onclick="handleFeedback(this,\'restaurant\',\'{safe_name}\',\'interested\')"><span>👀</span><span class="fb-label">Interested</span></button>\n'
            f'            <button class="feedback-btn" onclick="handleFeedback(this,\'restaurant\',\'{safe_name}\',\'swap\')"><span>🔄</span><span class="fb-label">Swap</span></button>\n'
            f'          </div>\n'
            f'        </div>'
        )

    html_parts.append('      </div>')
    html_parts.append('    </div>')
    return "\n".join(html_parts)


def _render_dn_slots(reservation, target_day, date_str, party_size):
    if not reservation:
        return ''

    error = reservation.get('error')
    platform = reservation.get('platform', '')
    booking_url = reservation.get('booking_url', '')
    phone = reservation.get('phone', '')

    if error == 'no_platform':
        if phone:
            return f'          <div class="dn-slots"><a href="tel:{phone}" class="reservation-link">📞 Call to book</a></div>\n'
        return ''

    if error == 'tock_no_scrape':
        if booking_url:
            link = build_booking_link('Tock', booking_url, date_str, party_size=party_size)
            return f'          <div class="dn-slots"><a href="{link}" target="_blank" class="slot-pill">Check Tock →</a></div>\n'
        return ''

    if error:
        if booking_url:
            return f'          <div class="dn-slots"><a href="{booking_url}" target="_blank" class="slot-pill">Check {platform} →</a></div>\n'
        return ''

    slots_key = f'{target_day}_slots'
    slots = reservation.get(slots_key, [])

    if slots:
        slot_links = []
        for slot in slots[:4]:
            link = build_booking_link(platform, booking_url, date_str, slot, party_size)
            slot_links.append(f'<a href="{link}" target="_blank" class="slot-pill">{slot}</a>')
        return f'          <div class="dn-slots">{" ".join(slot_links)}</div>\n'

    if booking_url:
        link = build_booking_link(platform, booking_url, date_str, party_size=party_size)
        return f'          <div class="dn-slots"><a href="{link}" target="_blank" class="slot-pill">Check {platform} →</a></div>\n'
    if phone:
        return f'          <div class="dn-slots"><a href="tel:{phone}" class="reservation-link">📞 Call to book</a></div>\n'
    return ''
