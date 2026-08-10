# ADHD-Informed Feature Prioritization for Hello World

## Executive Summary

Analyzes ADHD feature requests through ADHD psychology research. Classifies each as **foundational** (must shape core architecture day one) or **additive** (layer on later). Goal: embed right data structures, interaction patterns, extensibility points so ADHD accommodations are native, not retrofitted.

---

## Classification: Foundational vs. Additive

### Foundational Features (Must Shape Core Architecture)

Address most disabling ADHD symptoms. Require data structures, interaction patterns, or architectural decisions expensive to retrofit.

| Priority | Feature | Core ADHD Mechanism |
|----------|---------|---------------------|
| F1 | Shame Prevention / RSD-Aware Messaging | Rejection Sensitive Dysphoria |
| F2 | Decision Fatigue Prevention | Executive Function Depletion |
| F3 | Task Initiation Rewards | Dopamine-Deficient Reward System |
| F4 | Time Blindness Compensation | Impaired Time Perception |

### Additive Features (Can Be Layered On Later)

Enhance experience without fundamental architectural changes. Extend patterns from foundational features.

| Priority | Feature | Core ADHD Mechanism |
|----------|---------|---------------------|
| A1 | Transition/Task-Switching Support | Cognitive Flexibility Deficits |
| A2 | Waiting Mode Recognition | Anticipatory Paralysis |
| A3 | Novelty in Rewards | Habituation / Stimulation Seeking |
| A4 | Body Doubling / Accountability | Social Dopamine Stimulation |

---

## Interaction Patterns Required from Day One

**Zero-question intake as default:** First task intake works with zero clarifying questions. AI infers everything, confirms with accept/reject. Too vague → up to 3 questions max, last resort only.

**Shame-free messaging as hard constraint:** Every response template and AI prompt reviewed against RSD criteria. Never:
- Use "you should have," "you didn't," or "you failed"
- Imply deadline missed or user behind
- Frame task rejection as negative
- Use countdown language that creates pressure

**Celebrate starting, not just finishing:** First time user accepts task, system acknowledges initiation effort with brief warm response distinct from completion celebration.

## AI Prompt Constraints

Add to the base system prompt:

```
SHAME PREVENTION (MANDATORY):
- Never imply the user has failed or fallen short
- Frame all difficulties as information, not shortcomings
- If user rejects 3+ tasks, explicitly normalize: "Sometimes the brain isn't in task mode"
- If user can't finish, always acknowledge progress first
- If user seems frustrated, offer a graceful exit without judgment
- Never use urgency language that creates pressure or guilt
- Rejection is the user helping you suggest better -- say so

DECISION MINIMIZATION (MANDATORY):
- Default to zero clarifying questions during intake
- Infer all labels from context; confirm with accept/reject, not open questions
- If you must ask one question, offer 2-3 constrained choices, not open-ended
- Accept imprecise information -- a roughly-labeled task is better than an abandoned intake
```