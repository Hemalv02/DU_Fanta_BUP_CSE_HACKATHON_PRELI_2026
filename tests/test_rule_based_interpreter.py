"""Rule-based fallback interpreter: Section 4.2 examples, public-pack phrasings,
and the paraphrase variations from Section 11.4.
"""

import pytest

from app.repositories.llm.rule_based import RuleBasedLLMRepository

REPO = RuleBasedLLMRepository()


def interpret(note: str, capacity: float = 200.0) -> dict:
    return REPO.interpret_notes([note], capacity)[0]


class TestSectionFourExamples:
    @pytest.mark.parametrize(
        ("note", "directive", "hours", "value", "value_key"),
        [
            (
                "Solar output will drop to about 20% from 1 PM to 3 PM.",
                "solar_reduction",
                [13, 14],
                0.2,
                "factor",
            ),
            (
                "Do not charge the battery between 2 PM and 4 PM.",
                "no_charge_window",
                [14, 15],
                None,
                None,
            ),
            (
                "Keep at least 120 kWh in reserve from 6 PM until 9 PM.",
                "minimum_battery_reserve",
                [18, 19, 20],
                120.0,
                "minimum_energy_kwh",
            ),
            ("The cafeteria menu changes tomorrow.", "no_op", None, None, None),
        ],
    )
    def test_example(self, note, directive, hours, value, value_key) -> None:
        result = interpret(note)
        assert result["directive_type"] == directive
        assert result["applies"] == (directive != "no_op")
        adjustment = result["structured_adjustment"]
        if hours is None:
            assert adjustment is None
        else:
            assert adjustment["hours"] == hours
            if value_key is not None:
                assert adjustment[value_key] == pytest.approx(value, abs=0.01)


class TestHiddenWordingParaphrases:
    """Section 11.4: the same rule phrased differently."""

    @pytest.mark.parametrize(
        "note",
        [
            "PV production will drop to about 20% between 13:00 and 15:00.",
            "Panel washing from one until three will leave roughly "
            "one-fifth of normal solar output.",
            "Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.",
        ],
    )
    def test_solar_paraphrases(self, note: str) -> None:
        result = interpret(note)
        assert result["directive_type"] == "solar_reduction"
        assert result["structured_adjustment"]["hours"] == [13, 14]
        assert result["structured_adjustment"]["factor"] == pytest.approx(0.2, abs=0.01)

    def test_fraction_word_quarter(self) -> None:
        result = interpret(
            "Cloud inspection from 8 AM until 10 AM will leave a quarter of panel output."
        )
        assert result["directive_type"] == "solar_reduction"
        assert result["structured_adjustment"]["hours"] == [8, 9]
        assert result["structured_adjustment"]["factor"] == pytest.approx(0.25, abs=0.01)


class TestPublicPackPhrasings:
    def test_charger_isolated(self) -> None:
        result = interpret(
            "The battery charger will be isolated from 2 AM until 5 AM "
            "for electrical maintenance."
        )
        assert result["directive_type"] == "no_charge_window"
        assert result["structured_adjustment"]["hours"] == [2, 3, 4]

    def test_charging_circuit_unavailable(self) -> None:
        result = interpret("The charging circuit will be unavailable from 2 PM until 4 PM.")
        assert result["directive_type"] == "no_charge_window"
        assert result["structured_adjustment"]["hours"] == [14, 15]

    def test_must_not_discharge(self) -> None:
        result = interpret(
            "For protection testing, the battery must not discharge from 6 PM until 8 PM."
        )
        assert result["directive_type"] == "no_discharge_window"
        assert result["structured_adjustment"]["hours"] == [18, 19]

    def test_reserve_percentage_of_capacity(self) -> None:
        result = interpret(
            "Keep at least 50% of the battery capacity stored in the battery "
            "from 6 PM until 9 PM for emergency operations.",
            capacity=200.0,
        )
        assert result["directive_type"] == "minimum_battery_reserve"
        assert result["structured_adjustment"]["hours"] == [18, 19, 20]
        reserve = result["structured_adjustment"]["minimum_energy_kwh"]
        assert reserve == pytest.approx(100.0, abs=0.01)

    def test_grid_at_or_below(self) -> None:
        result = interpret(
            "Grid intake must stay at or below 190 kWh from 7 PM until 10 PM "
            "while the substation is constrained."
        )
        assert result["directive_type"] == "max_grid_window"
        assert result["structured_adjustment"]["hours"] == [19, 20, 21]
        assert result["structured_adjustment"]["max_grid_kwh"] == pytest.approx(190.0, abs=0.01)

    def test_transformer_limit_before_value(self) -> None:
        result = interpret(
            "The evening transformer limit is 180 kWh of grid import from 7 PM until 9 PM."
        )
        assert result["directive_type"] == "max_grid_window"
        assert result["structured_adjustment"]["hours"] == [19, 20]
        assert result["structured_adjustment"]["max_grid_kwh"] == pytest.approx(180.0, abs=0.01)

    def test_data_center_reserve(self) -> None:
        result = interpret(
            "The data center requires at least 80 kWh to remain in the battery "
            "from 6 PM until 10 PM."
        )
        assert result["directive_type"] == "minimum_battery_reserve"
        assert result["structured_adjustment"]["hours"] == [18, 19, 20, 21]
        reserve = result["structured_adjustment"]["minimum_energy_kwh"]
        assert reserve == pytest.approx(80.0, abs=0.01)

    def test_noon_window(self) -> None:
        result = interpret(
            "Facilities will wash the rooftop solar panels from noon until 2 PM. "
            "During cleaning, usable solar should be treated as roughly 25% of the forecast."
        )
        assert result["directive_type"] == "solar_reduction"
        assert result["structured_adjustment"]["hours"] == [12, 13]
        assert result["structured_adjustment"]["factor"] == pytest.approx(0.25, abs=0.01)

    def test_half_remaining(self) -> None:
        result = interpret(
            "Cloud cover during panel inspection will leave about half of the "
            "forecast solar output from 10 AM until noon."
        )
        assert result["directive_type"] == "solar_reduction"
        assert result["structured_adjustment"]["hours"] == [10, 11]
        assert result["structured_adjustment"]["factor"] == pytest.approx(0.5, abs=0.01)


class TestDistractors:
    @pytest.mark.parametrize(
        "note",
        [
            "The sports office moved next month's registration deadline.",
            "The library is extending book-return hours next week.",
            "The student affairs office will publish club notices tomorrow.",
            "A seminar room booking was moved to next week.",
            "HR will host a team lunch on Friday.",
        ],
    )
    def test_no_op(self, note: str) -> None:
        result = interpret(note)
        assert result["directive_type"] == "no_op"
        assert result["applies"] is False
        assert result["structured_adjustment"] is None


class TestWindowSemantics:
    def test_start_inclusive_end_exclusive(self) -> None:
        result = interpret("Do not charge the battery between 9 AM and 11 AM.")
        assert result["structured_adjustment"]["hours"] == [9, 10]

    def test_crossing_midnight(self) -> None:
        result = interpret(
            "Avoid charging the battery from 11 PM to 1 AM during grid works."
        )
        assert result["structured_adjustment"]["hours"] == [23, 0]
