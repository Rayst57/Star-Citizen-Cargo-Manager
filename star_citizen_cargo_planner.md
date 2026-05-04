# Star Citizen Cargo Route Planner (C2 Hercules Optimized)

## Overview

This application is a voice-driven cargo logistics planner for Star Citizen, designed specifically for large cargo vessels such as the C2 Hercules.

The system allows a user to:
- Input contracts via voice
- Automatically parse and structure contract data
- Optimize a multi-stop cargo route
- Generate a cargo loading plan based on ship layout
- Assign cargo zones for efficient unloading
- Output results in readable and exportable formats

---

## Core Features

### 1. Voice Input System
- Capture spoken contract data
- Convert speech → text using OpenAI Speech-to-Text
- Trigger parsing when user says:
  - "contract list complete"

---

### 2. Contract Parsing Engine

#### Input (raw text)
Example:
"Contract one starts at Seraphim, collect 5 SCU processed food to Ambitious Dream"

#### Output (structured JSON)
```json
{
  "contract_id": 1,
  "origin": "Seraphim Station",
  "max_pallet_size": 4,
  "cargo": [
    {
      "commodity": "Processed Food",
      "scu": 5,
      "destination": "Ambitious Dream"
    }
  ]
}
```

#### Responsibilities
- Extract:
  - Origin
  - Commodity
  - SCU amount
  - Destination
  - Max pallet size
- Normalize station names
- Handle corrections like:
  - "scratch that"
  - overwrite previous contract

---

### 3. Commodity Handling Logic

#### Supported SCU Sizes
- 1, 2, 4, 8, 16, 24, 32

#### Palletization Rules
- Use largest possible pallet ≤ contract max size
- Fill remainder with smaller pallets

Example:
- 5 SCU, max pallet 4 → [4,1]
- 17 SCU, max pallet 8 → [8,8,1]

---

### 4. Ship Model (C2 Hercules)

#### Total Capacity
- 696 SCU

#### Zone Layout

**Forward Section**
- F1
- F2
- F3

**Rear Section**
- R1 (first offload)
- R2
- R3
- R4

---

### 5. Cargo Zoning Strategy

#### Rule: Unload Efficiency

| Zone | Purpose |
|------|--------|
| R1   | First stop cargo |
| R2   | Second stop |
| R3   | Third stop |
| R4   | Mid-route |
| F1   | Late route |
| F2   | Final stop |
| F3   | Return cargo |

#### Key Principle
> Cargo should be loaded in reverse order of delivery.

---

### 6. Route Optimization Engine

#### Goals
- Minimize backtracking
- Group destinations geographically
- Create a loop returning to origin

#### Inputs
- Contract list
- Known station map (Stanton system)

#### Outputs
Ordered stop list

---

### 7. Cargo Assignment Engine

#### Responsibilities
- Assign pallets to zones
- Track SCU usage per zone
- Prevent overflow

---

### 8. Stop Execution Format

Each stop includes:

STOP: [Station]

UNLOAD:
- Zone cargo

LOAD:
- Cargo + destination + zone

---

### 9. Output Formats

- Text Route Plan
- Cargo Grid Layout
- CSV / Excel / PDF export

---

### 10. Voice Feedback (Optional)

- Read back route summary
- Confirm parsed contracts
- Notify errors

---

## System Architecture

### Frontend
- React
- Voice capture UI
- Route display UI
- Cargo visualization

### Backend
- Node.js or FastAPI
- Handles parsing, optimization, cargo logic

### OpenAI Integration
- Speech-to-text
- Structured parsing (LLM)
- Optional voice output

---

## Data Models

### Contract
```json
{
  "id": number,
  "origin": string,
  "max_pallet_size": number,
  "cargo": []
}
```

### Cargo Item
```json
{
  "commodity": string,
  "scu": number,
  "destination": string
}
```

### Pallet
```json
{
  "size": number,
  "commodity": string,
  "destination": string
}
```

---

## Future Enhancements

- Profit optimization
- Fuel/time estimation
- Multi-ship coordination
- Live Star Citizen API integration
- Drag-and-drop cargo planner
- 3D cargo bay visualization

---

## Key Design Philosophy

This is not just a route planner — it is a cargo operations system.

Focus on:
- Real pilot workflow
- Minimal cargo reshuffling
- Maximum efficiency per stop

---

## End Goal

A system where a pilot can say:

"Here are my contracts..."

…and receive:
- A complete route
- A perfect cargo load plan
- A step-by-step execution guide

—all within seconds.
