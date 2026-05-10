"""Render functions for the "Coming Up" tab: radar event cards."""

import datetime


def parse_event_date(date_str):
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def render_coming_up_card(event, notes_text):
    date_str = event.get("Date", "")
    d = parse_event_date(date_str)
    if d:
        month_str = d.strftime("%b").upper()
        day_num = d.day
        dow_str = d.strftime("%a")
    else:
        month_str = "TBD"
        day_num = "?"
        dow_str = ""
    name = event.get("Name", "Event")
    venue = event.get("Venue", "")
    record_id = event.get("_record_id", "")
    conflicts = event.get("calendar_conflicts", [])
    if conflicts:
        first = conflicts[0]
        conflict_text = f'⚠️ You have {first.get("summary","")} ({first.get("calendar","")}) that day'
        if len(conflicts) > 1:
            conflict_text += f' +{len(conflicts)-1} more'
        conflict_html = f'<div class="conflict-banner">{conflict_text}</div>'
    else:
        conflict_html = '<div class="clear-banner">✅ Calendar looks clear</div>'
    notes_html = f'<div class="coming-up-notes">{notes_text}</div>' if notes_text else ''
    chips_html = ''
    if event.get("Price"):
        chips_html += f'<span class="chip chip-price">{event["Price"]}</span>'
    if event.get("Neighborhood"):
        chips_html += f'<span class="chip chip-neighborhood">{event["Neighborhood"]}</span>'
    chip_row = f'<div class="chip-row">{chips_html}</div>' if chips_html else ''
    safe_name = name.replace("'", "&#39;")
    feedback_html = (
        '    <div class="feedback-row">\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'event\',\'{safe_name}\',\'love\')"><span>❤️</span><span class="fb-label">Love</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'event\',\'{safe_name}\',\'nope\')"><span>👎</span><span class="fb-label">Nope</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'event\',\'{safe_name}\',\'interested\')"><span>👀</span><span class="fb-label">Interested</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'event\',\'{safe_name}\',\'swap\')"><span>🔄</span><span class="fb-label">Swap</span></button>\n'
        '    </div>'
    )
    return (
        f'    <div class="coming-up-card" data-record-id="{record_id}" data-name="{name}" data-type="event">\n'
        f'      <div class="coming-up-header">\n'
        f'        <div class="coming-up-date-block">\n'
        f'          <div class="coming-up-month">{month_str}</div>\n'
        f'          <div class="coming-up-day-num">{day_num}</div>\n'
        f'          <div class="coming-up-dow">{dow_str}</div>\n'
        f'        </div>\n'
        f'        <div class="coming-up-info">\n'
        f'          <div class="coming-up-name">{name}</div>\n'
        f'          <div class="coming-up-venue">{venue}</div>\n'
        f'        </div>\n'
        f'      </div>\n'
        f'      <div class="coming-up-body">\n'
        f'        {conflict_html}\n'
        f'        {notes_html}\n'
        f'        {chip_row}\n'
        f'{feedback_html}\n'
        f'      </div>\n'
        f'    </div>'
    )


def render_coming_up_html(radar_events, coming_up_notes):
    if not radar_events:
        return '    <div class="calendar-empty">Nothing on the radar yet</div>'
    return "\n".join(
        render_coming_up_card(ev, coming_up_notes.get(ev.get("Name", ""), ""))
        for ev in radar_events
    )
