"""载具经核实的燃料规格；这里不创建、移动或消耗任何实物。"""

VEHICLE_FUEL_PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_fuel_profiles (
    vehicle_id TEXT PRIMARY KEY REFERENCES vehicles(id),
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    item_type_id TEXT NOT NULL REFERENCES item_types(id),
    capacity_units INTEGER NOT NULL CHECK(typeof(capacity_units)='integer' AND capacity_units BETWEEN 1 AND 1000000000),
    rate_numerator INTEGER NOT NULL CHECK(typeof(rate_numerator)='integer' AND rate_numerator BETWEEN 1 AND 1000000000),
    rate_denominator INTEGER NOT NULL CHECK(typeof(rate_denominator)='integer' AND rate_denominator BETWEEN 1 AND 1000000000),
    registration_id TEXT NOT NULL REFERENCES element_registration_requests(id)
);
"""


def install_vehicle_fuel_profiles(connection):
    """哥哥，逐条建表，保留调用方的事务。"""
    for statement in VEHICLE_FUEL_PROFILE_SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)
