"""哥哥，未来请客只记录明确承诺，到场后才使用实际食物。"""

APPOINTMENT_MEAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS appointment_meal_offers (
 appointment_id TEXT PRIMARY KEY REFERENCES social_appointments(id) ON DELETE CASCADE,
 provider_id TEXT NOT NULL REFERENCES characters(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id)
);
"""


class AppointmentMealService:
    @staticmethod
    def terms(c, appointment_id):
        row = c.execute("SELECT provider_id FROM appointment_meal_offers WHERE appointment_id=?", (appointment_id,)).fetchone()
        return {"meal_supply": "proposer" if row else "each_own", "meal_provider_id": row[0] if row else None}

    @classmethod
    def provider(cls, c, actor, choice, current=None):
        if choice.kind != "meal":
            return None
        if choice.meal_supply is None and current is not None:
            return cls.terms(c, current["id"])["meal_provider_id"]
        return actor["id"] if choice.meal_supply == "proposer" else None

    @staticmethod
    def save(c, appointment_id, provider_id, event_id):
        if provider_id:
            c.execute("INSERT INTO appointment_meal_offers VALUES (?,?,?)", (appointment_id, provider_id, event_id))
