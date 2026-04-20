import threading
import datetime
import pytz
import logging

TZ = pytz.timezone("Europe/Berlin")
log = logging.getLogger("planner")


class DayPlanner(threading.Thread):
    def __init__(self, tado, zones):
        super().__init__(daemon=True)
        self.tado = tado
        self.zones = zones  # room -> zone_id
        self.events = []
        self.stop_event = threading.Event()
        self.finished = False
        self.immediate = False  # now=true -> nicht warten

    def _target_date(self):
        """
        Standard: nächster Tag
        Ausnahme: vor 05:00 → heute
        """
        now = datetime.datetime.now(TZ)
        if now.hour < 5:
            return now.date()
        return (now + datetime.timedelta(days=1)).date()

    def load_plan(self, plan):
        self.events.clear()
        target_date = self._target_date()

        for room, entries in plan["rooms"].items():
            zone = self.zones[room]

            for e in entries:
                t = datetime.datetime.strptime(e["time"], "%H:%M").time()
                dt = TZ.localize(datetime.datetime.combine(target_date, t))
                self.events.append((dt, zone, e["temp"]))

        self.events.sort(key=lambda x: x[0])

        log.info(
            "Planner loaded %d events for %s (immediate=%s)",
            len(self.events),
            target_date.isoformat(),
            self.immediate,
        )

    def abort(self):
        log.info("Planner abort requested")
        self.stop_event.set()

    def run(self):
        try:
            for when, zone, temp in self.events:
                if self.stop_event.is_set():
                    log.info("Planner aborted")
                    return

                if not self.immediate:
                    now = datetime.datetime.now(TZ)
                    wait_seconds = (when - now).total_seconds()

                    if wait_seconds > 0:
                        log.info(
                            "Waiting %.1f seconds for next event (%s)",
                            wait_seconds,
                            when.strftime("%H:%M"),
                        )
                        self.stop_event.wait(wait_seconds)

                    if self.stop_event.is_set():
                        log.info("Planner aborted during wait")
                        return
                else:
                    log.info("IMMEDIATE: applying event (%s)", when.strftime("%H:%M"))

                try:
                    if temp == 0:
                        log.info("Zone %s → frost protection", zone)
                        self.tado.set_frost_protection(zone)
                    else:
                        log.info("Zone %s → manual %.1f °C", zone, temp)
                        self.tado.set_manual_temperature(zone, temp)
                except Exception:
                    # damit der Thread nicht stirbt
                    log.exception("Failed to apply event for zone %s", zone)

            log.info("Planner finished all events")

        finally:
            self.finished = True
