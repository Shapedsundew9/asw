# **Vision Document: AgenticOrg CLI**

**Author:** Founder

**Date:** April 14, 2026

**Target Phase:** V0.1 & V0.2 Foundation

## **1\. Product Overview**

I want to build "AgenticOrg CLI", a Python-based command-line interface tool that orchestrates a simulated company of LLM-based software development agents. The tool will live entirely within a VS Code DevContainer and utilize standard Python 3.14+ and Bash scripts to manage a complete Software Development Life Cycle (SDLC).

## **2\. Target Audience**

Solo founders, independent developers, and small engineering teams who want to leverage AI for end-to-end product development without sacrificing architectural integrity, security, or testing standards.

## **3\. Core Mechanics & Requirements**

The CLI must enforce the following strict mechanical loops:

* **The DevContainer Mandate:** All operations, agent executions, and file generation must happen within an isolated Docker/DevContainer environment to ensure reproducibility.  
* **The Git State Machine:** The CLI must mechanically commit the state of the .company directory and src/ directory at the end of every successful agentic phase.  
* **The Founder Review Gate:** At every major phase transition, the CLI must pause and present a terminal prompt allowing the user to \[A\]pprove, \[R\]eject, \[M\]odify, or \[S\]top.  
* **Mechanical Linting:** The CLI must parse agent outputs (Markdown) to mechanically verify the presence of templates, completed checklists (- \[x\]), and valid Mermaid.js diagrams before allowing a phase to pass.

## **4\. Expected Agent Personas**

The system must support the dynamic instantiation of distinct agent roles, guided by system prompts stored in .company/roles/. The primary roles to support in the MVP are:

* **Product Manager (CPO):** Translates this vision into a PRD.  
* **Engineering Manager (CTO):** Translates the PRD into system architecture and tech stack requirements.  
* **Hiring Manager:** Dynamically generates system prompts for downstream agents.

## **5\. Technology Constraints**

* The orchestrator itself will be written in pedantic, strictly-typed **Python 3.12+**.  
* It will use standard libraries (e.g., argparse, subprocess, re, json) rather than heavy external agent frameworks.  
* All generated documentation must be in **Markdown**.  
* All diagrams must be in embedded **Mermaid** markdown.  
* The default design aesthetic for any generated UI code will be a **Dark Theme** with easy-on-the-eye colors and shading.

## **6\. Definition of Done (V0.1 MVP)**

The V0.1 MVP is successful when I can run asw start \--vision vision.md, and the system automatically coordinates the CPO and CTO agents to produce a linted, mechanically validated, and git-committed Product Requirements Document and Architecture JSON file, pausing for my approval via the terminal.
