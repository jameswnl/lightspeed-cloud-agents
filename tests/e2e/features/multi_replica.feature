Feature: Multi-replica workflow runner
  As a platform operator
  I want workflows to survive replica failures
  So that the system is resilient and stateless (R7)

  Background:
    Given a Kind cluster is running
    And the 2-replica workflow runner overlay is deployed
    And both workflow runner replicas are ready

  Scenario: Workflow survives replica crash
    Given a multi-step workflow is submitted
    And the workflow reaches the "diagnose" step
    When I delete the pod running the active workflow
    Then Temporal re-dispatches the activity to the surviving replica
    And the workflow completes end-to-end with all steps succeeded

  Scenario: Orphan cleanup across replicas
    Given replica A has a running sandbox container
    When replica A is killed
    And a replacement replica starts
    Then the replacement replica runs orphan reconciliation on startup
    And the orphaned sandbox from replica A is cleaned up

  Scenario: Concurrent workflows across replicas
    Given 4 workflows are submitted simultaneously
    Then all 4 workflows complete successfully
    And workflows were distributed across both replicas
