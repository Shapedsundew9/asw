# Design Plan: Phase Implementation Loop (YOLO Mode + Dev Lead Review)

## 1. Overview & Objectives

We need to implement the core "Implementation Loop" for the SDLC pipeline within `src/asw/orchestrator.py`. This loop executes after the phase design and DevOps setup are completed.

**The flow for each phase:**

1. Iterate sequentially through each role assigned to the phase.
2. For each role, enter a `while True:` iteration loop.
3. **Plan:** Prompt the role to generate an implementation plan using the Gemini CLI "plan" capability.
4. **Execute:** Run the generated plan using the Gemini CLI "execute" capability in YOLO (Auto-Approve / Non-interactive) mode.
5. **Review:** Gather the file mutations (git diff) and submit them to the **Development Lead** for review.
6. **Evaluate:** - If the Dev Lead approves, break the loop, commit the role's work, and move to the next role.
   - If the Dev Lead requests changes (deltas found against coding/testing standards or phase design), pass the feedback into the next iteration of the role's Plan/Execute steps.

## 2. LLM Backend & Agent Extensions (`src/asw/llm/gemini.py` & `src/asw/agents/base.py`)

The orchestrator currently assumes a single `invoke` / `run` text-generation call. We need to expose the Gemini CLI's plan and execute capabilities.

- **`GeminiCLIBackend` Updates:**
  - Add an `invoke_plan(self, system_prompt: str, user_prompt: str) -> str` method. This should wrap the specific Gemini CLI command/flag used for planning.
  - Add an `invoke_execute(self, plan: str, context: dict, auto_approve: bool = True) -> str` method. This wraps the Gemini CLI command for executing code changes, passing whatever flag triggers your "YOLO / auto-approve" mode.
- **`Agent` Updates:**
  - Add `plan(self, context: dict, feedback: str | None = None) -> str`.
  - Add `execute(self, plan: str, auto_approve: bool = True) -> str`.

## 3. Pipeline State Management (`src/asw/orchestrator.py`)

We must track the implementation state per-role to allow the pipeline to resume safely if interrupted.

- **State Keys:** Use the existing `_phase_loop_state_name` helper.
  - Key format: `_phase_loop_state_name(phase_id, f"implement-{role_title}")`.
- **Artifacts:** The outputs of this phase are the mutated source code files. Instead of tracking specific output files (which are dynamic), we will rely on git commits and the pipeline state dictionary to mark a role's implementation as `completed`.

## 4. Orchestrator Logic (`src/asw/orchestrator.py`)

### A. The Development Lead Review Linter

Create a function to parse the Development Lead's review output. The prompt should force a JSON structure.

```python
def _lint_dev_lead_review(content: str) -> tuple[list[str], dict | None]:
    # Extracts JSON block and validates it contains "status" ("approved" | "rejected") 
    # and "feedback" (string).
    pass
```

### B. The Role Implementation Step

Create a new function `_run_role_implementation_loop(...)` that handles the inner iteration for a single role.

```python
def _run_role_implementation_loop(
    exec_ctx: PipelineExecutionContext,
    company: Path,
    phase_data: dict,
    role_entry: dict,
    final_design_content: str,
    architecture_json: str,
    llm: LLMBackend,
) -> None:
    role_title = role_entry["title"]
    agent = Agent(name=role_title, role_file=company / "roles" / role_entry["filename"], llm=llm)
    
    # Core Development Lead agent for review
    dev_lead = Agent(name="Development Lead Reviewer", role_file=company / "roles" / "development_lead.md", llm=llm)
    
    feedback = None
    attempt = 1
    
    while True:
        # 1. PLAN
        plan_context = {
            "task": f"Implement your assigned tasks for phase: {phase_data.get('name')}",
            "phase_design": final_design_content,
            "architecture": architecture_json,
            # include role-specific standards here
        }
        plan = agent.plan(plan_context, feedback=feedback)
        
        # 2. EXECUTE (YOLO)
        # Capture git hash/state before execution to compute the diff later
        before_hash = get_current_git_head(exec_ctx.workdir) 
        
        print(f">> {role_title} executing code changes (YOLO mode)...")
        agent.execute(plan, auto_approve=True)
        
        # 3. REVIEW PREPARATION
        # Get the diff of what the agent actually changed
        diff = get_git_diff(exec_ctx.workdir, since=before_hash)
        
        if not diff.strip():
            print(f"⚠ {role_title} made no file changes. Prompting for retry.")
            feedback = "Your execution resulted in no file changes. Please review the design and implement the required code."
            attempt += 1
            continue

        # 4. DEV LEAD REVIEW
        review_context = {
            "review_request": (
                "Review the provided git diff against the Phase Design and project coding standards. "
                "Return a JSON object with 'status' ('approved' or 'rejected') and 'feedback'."
            ),
            "phase_design": final_design_content,
            "agent_diff": diff,
            "role_evaluated": role_title,
        }
        
        review_output = _agent_loop(
            dev_lead, 
            review_context, 
            lambda c: _lint_dev_lead_review(c)[0], 
            f"Dev Lead Review: {role_title}"
        )
        _, review_json = _lint_dev_lead_review(review_output)
        
        # 5. EVALUATE
        if review_json["status"] == "approved":
            print(f"✓ Development Lead approved {role_title}'s implementation.")
            # Commit the role's work to git here
            _try_commit(exec_ctx.workdir, f"implementation-{role_title}", exec_ctx.options.no_commit, stage_all=True)
            break
        else:
            print(f"↺ Development Lead requested changes for {role_title}.")
            feedback = f"Development Lead Review Failed. Fix the following issues:\n{review_json['feedback']}"
            
            # Revert the working directory to `before_hash` so the agent tries again cleanly, 
            # OR leave the files modified and let the agent fix them forward (Fix-forward is usually better for LLMs).
            attempt += 1
```

### C. Integrating into the Phase Loop

Update `_run_phase_preparation_loop` (or rename it to `_run_phase_loop` since it now does implementation too).

```python
def _run_phase_loop(
    exec_ctx: PipelineExecutionContext,
    # ... other args ...
):
    for phase_index, phase_data in enumerate(phases):
        # ... existing design and devops steps ...
        
        # --- NEW IMPLEMENTATION LOOP ---
        team_entries = _phase_team_entries(roster_json, phase_data)
        for role_entry in team_entries:
            role_key = _phase_loop_state_name(phase_data.get("id"), f"implement-{role_entry['title']}")
            
            if _is_phase_done(exec_ctx.state, role_key, [], workdir=exec_ctx.workdir):
                print(f"↩ Skipping {role_entry['title']} implementation (already completed)")
                continue
                
            _run_role_implementation_loop(
                exec_ctx,
                company=exec_ctx.company,
                phase_data=phase_data,
                role_entry=role_entry,
                final_design_content=final_design,
                architecture_json=architecture_json,
                llm=exec_ctx.llm
            )
            
            mark_phase_complete(exec_ctx.workdir, exec_ctx.state, role_key, input_paths=[], output_paths=[])
```

## 5. Required Prompts & Templates

Instruct the coding agent to add the following specific instructions to the Development Lead's evaluation payload:

- **Strict Review Criteria:** Ensure the Dev Lead explicitly checks testing coverage, syntax rules (from `.company/standards/`), and phase boundary compliance (did the agent build *more* than the phase design asked for?).
- **JSON Format Requirement:** The Dev Lead must wrap their verdict in a
