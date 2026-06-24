import numpy as np
import pytest
from lbs_predictor.config import Settings
from lbs_predictor.sufficiency import (
    calculate_average_response_time,
    optimize_placements,
    solve_sufficiency,
)


def test_calculate_average_response_time():
    settings = Settings()
    # Simple coords: one point at (0, 0), one at (0.1, 0.1)
    coords = np.array([[0.0, 0.0], [0.1, 0.1]])
    weights = np.array([10, 10])

    # Case 1: Medoid is exactly at the centroid/medoid
    # If medoids = [[0.0, 0.0]], calculate average response time
    medoids = np.array([[0.0, 0.0]])
    avg_rt = calculate_average_response_time(coords, weights, medoids, settings)
    assert avg_rt > 0

    # Case 2: Two medoids, each point has its own medoid
    medoids_two = np.array([[0.0, 0.0], [0.1, 0.1]])
    avg_rt_two = calculate_average_response_time(coords, weights, medoids_two, settings)
    # Average response time when each point is its own medoid should be 0.0
    assert avg_rt_two == 0.0


def test_optimize_placements():
    settings = Settings()
    coords = np.array([[0.0, 0.0], [0.1, 0.1], [0.2, 0.2]])
    weights = np.array([10, 5, 2])

    # Optimize for 1 cluster
    medoids, avg_rt = optimize_placements(coords, weights, 1, settings)
    assert len(medoids) == 1
    assert avg_rt > 0

    # Optimize for 3 clusters (should be 0.0 response time since each point has a medoid)
    medoids_three, avg_rt_three = optimize_placements(coords, weights, 3, settings)
    assert len(medoids_three) == 3
    assert avg_rt_three == 0.0


def test_solve_sufficiency():
    settings = Settings()
    coords = np.array([[0.0, 0.0], [0.1, 0.1], [0.2, 0.2]])
    weights = np.array([100, 100, 100])

    # Solve for target response time of 5.0 minutes
    # Assume 1 FRV currently, find required FRVs for <= 5.0 minutes
    v_req, placements, rt = solve_sufficiency(coords, weights, v_curr=1, target_rt=5.0, settings=settings)
    assert v_req >= 1
    assert rt <= 5.0
    assert len(placements) == v_req
