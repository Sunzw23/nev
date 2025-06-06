from utils import extract_boxed, remove_think_tags, find_box, extract_tag_content, extract_all_tag_content, convert_memory_json_to_md, convert_memory_json_to_latex
from agents import Solver, Reviewer, ProofRefiner, Planner, SolverWithContext, VerifierWithContext, RefinerWithContext, Explorer, ExpReviewer, ExpRefiner
from typing import Optional
import os
import json
import logging
from typing import Union
import concurrent.futures

from tqdm import tqdm

def peval_pipeline(
    problems: Union[list[str], list[dict]],
    proofs: list[str],
    reviewer: str,
    reviews: int = 1,
    workers: int = 1
) -> dict:
    if isinstance(problems[0], dict):
        problems = [p['problem'] for p in problems]
    evaluate_prompts = [[pr, pf] for pr, pf in zip(problems, proofs) for _ in range(reviews)]
    reviewer_agent = Reviewer(reviewer)
    raw_reviews = reviewer_agent.batch_generate(evaluate_prompts, workers=workers)
    results = []
    for i in range(len(problems)):
        solved = True
        for j in range(reviews):
            if find_box(raw_reviews[i * reviews + j]) == "false":
                results.append({
                    'problem': problems[i],
                    'proof': proofs[i],
                    'review': remove_think_tags(raw_reviews[i * reviews + j]),
                    'judgement': False
                })
                solved = False
                break
            else:
                continue
        if solved:
            results.append({
                'problem': problems[i],
                'proof': proofs[i],
                'review': remove_think_tags(raw_reviews[i * reviews]),
                'judgement': True
            })
    return results

def prefine_pipeline(
    problems: Union[list[str], list[dict]],
    solver: str,
    reviewer: str,
    refiner: str,
    reviews: int = 1,
    iterations: int = 0,
    workers: int = 1
) -> list[dict]:
    """
    This is the batched prefine pipeline.
    You need to pass a list of problems to this function and it will return a list of dictionaries containing results.
    It will:
    1. Calls solver to get the proof.
    2. Iteratively evaluate this proof with pessimistic_vote and refines this proof.
    5. Returns a dictionary with all relevant logs.
    """
    if isinstance(problems[0], dict):
        problems = [p['problem'] for p in problems]
    solver_arguments = [[p] for p in problems]
    solver_agent = Solver(solver)
    raw_solutions = solver_agent.batch_generate(solver_arguments, workers=workers)
    proofs = [remove_think_tags(s) for s in raw_solutions]
    results = []

    results = peval_pipeline(problems, proofs, reviewer, reviews, workers)
    # refining process if needed
    for _ in range(iterations):
        solved_results = [r for r in results if r['judgement']]
        remaining_results = [r for r in results if not r['judgement']]
        refine_arguments = [[r['problem'], r['proof'], r['review']] for r in remaining_results]
        refiner_agent = ProofRefiner(refiner)
        raw_refined_proofs = refiner_agent.batch_generate(refine_arguments, workers=workers)
        refined_proofs = [extract_tag_content(remove_think_tags(rr), 'proof') for rr in raw_refined_proofs]
        remaining_problems = [r['problem'] for r in remaining_results]
        refined_results = peval_pipeline(remaining_problems, refined_proofs, reviewer, reviews, workers)
        results = solved_results + refined_results

    return results

class MathAgentPipeline():
    """
    The complete math agent pipeline, equiped with
    1. Planner for proposing new conjectures
    2. Solver for proving or disproving the conjecture
    3. Reviewer for assesing the proof generated by the solver
    4. Refiner for refining or rewriting proofs based on the feedback from the verifier
    5. Memory mechanism to consistantly collect and facilitate exploration of the open problem
    """
    def __init__(self,
                 method: str,
                 proof_model: str,
                 eval_model: str,
                 reform_model: str,
                 max_steps: int = 6,
                 reviews: int = 3,
                 refine_iterations: int = 2,
                 parallel_solve_iterations: int = 1,
                 log_dir: str = "samples",
                 log_per_steps: int = 10,
                 ):
        self.method = method
        self.proof_model = proof_model
        self.eval_model = eval_model
        self.reform_model = reform_model
        self.max_steps = max_steps
        self.current_steps = 0
        self.reviews = reviews
        self.refine_iterations = refine_iterations
        self.parallel_solve_iterations = parallel_solve_iterations
        self.log_dir = log_dir
        self.log_per_steps = log_per_steps

        self.memory = []
        if self.method == 'ma':
            self.planner = Planner(self.proof_model)
            self.solver = SolverWithContext(self.proof_model)
            self.reviewer = VerifierWithContext(self.eval_model)
            self.refiner = RefinerWithContext(self.proof_model)
        elif self.method == 'mas':
            self.explorer = Explorer(self.proof_model)
            self.reviewer = ExpReviewer(self.eval_model)
            self.refiner = ExpRefiner(self.proof_model)
        else:
            raise NotImplementedError("Unknown method in MathAgent.")

    def pessimistic_eval(self, conjecture: str, judgement: str, proof: str) -> Optional[str]:
        """
        This function evaluates the judgement and proof of the given conjecture.
        It will return:
        1. None if no flaw in the proof is found
        2. A string containing comments about the flaws in the proof
        """
        logging.info("Start evaluation with pessimistic verification")

        # parallel pessimistic evaluation
        if self.method == "ma":
            args = [(conjecture, judgement, proof, self.memory)] * self.reviews
        elif self.method == "mas":
            args = [(conjecture, proof, self.memory)] * self.reviews

        with concurrent.futures.ThreadPoolExecutor(max_workers = 1 if self.reviewer.debug else self.reviews) as executor:
            futures = [
                executor.submit(self.reviewer, *arg)
                for arg in args
            ]

            for future in tqdm(concurrent.futures.as_completed(futures),
                               total=self.reviews,
                               desc="pverify"):
                raw_review = future.result()
                if find_box(raw_review) == "invalid":
                    for f in futures:
                        f.cancel()
                    return raw_review

        return None

    def refine_proof(self, conjecture: str, judgement: str, proof: str, verification: str) -> tuple[str, str]:
        """
        This function refines the proof with given verification when the corresponding judgement or proof is not valid.
        It will return a new tuple containing:
        [new_judgement, new_proof]
        """
        logging.info("Start refining proof")
        raw_refinement = self.refiner(conjecture, judgement, proof, verification, self.memory)
        new_judgement = find_box(raw_refinement)
        new_proof = extract_tag_content(raw_refinement, "proof")
        return (new_judgement, new_proof)

    def refine_proof_mas(self, conjecture: str, proof: str, review: str) -> tuple[str, str]:
        """
        This function refines the proof in mas setting with given verification when the corresponding judgement or proof is not valid.
        It will return a new tuple containing:
        [new_conjecture, new_proof]
        """
        logging.info("Start refining proof")
        raw_refinement = self.refiner(conjecture, proof, review, self.memory)
        new_conjecture = extract_tag_content(raw_refinement, "conjecture")
        new_proof = extract_tag_content(raw_refinement, "proof")
        return (new_conjecture, new_proof)

    def update_memory(self,
                      type: str,
                      content: str,
                      correctness: Optional[str],
                      proof: Optional[str],
                      comment: Optional[str]):
        """
        We can update memory using this function.
        The short term memory used in an open problem need to be a dictionary with the following elements:
        {
            "type": "conjecture | context",
            "content": "natural language statement of this element",
            "correctness": "true | false | unknown", # the correctness of retrieved context will always be true 
            "proof": "proof of this conjecture, not included in formatted string but is useful as well",
            "comment": "comments on this element"
        }
        """
        logging.info(f"Memory updated with conjecture / context:\n{content}")
        self.memory.append({
            'type': type,
            'content': content,
            'correctness': correctness,
            'proof': proof,
            'comment': comment
        })

    def explore_iteration(self, problem: str) -> Optional[bool]:
        """
        Explore for proof of this problem for one step.
        It will propose a new goal, solve and verify it to update its memory.
        There is three possible return values of this function:
        1. true. The given problem is solved and the correctness is true.
        2. false. The given problem is solved and the correctness is false.
        3. None. The given problem is not solved yet, and we need another iteration to finally solve this problem.
        """
        raw_plan = self.planner(problem, self.memory)
        final_result = extract_boxed(raw_plan)

        # Directly return the result if the solution is already found.
        if final_result is not None and final_result == "solved":
            logging.info("Found a final answer to this problem!")
            return True

        # Or else we will try to solve this conjecture.
        conjecture = extract_tag_content(raw_plan, "conjecture")
        if conjecture is None:
            logging.error("Failed to extract the conjecture!")
            return None
        
        solved = False
        parallel_solves = 0
        while not solved and parallel_solves < self.parallel_solve_iterations:
            parallel_solves += 1
            logging.info(f"Trying to solve conjecture:\n{conjecture}")
            raw_proof = self.solver(conjecture, self.memory)
            judgement = find_box(raw_proof)
            proof = extract_tag_content(raw_proof, "proof")
            if judgement is None or proof is None:
                continue

            # Pessimistic Verification and Refining
            for _ in range(self.refine_iterations):
                verification = self.pessimistic_eval(conjecture, judgement, proof)
                if verification is None:
                    solved = True
                    # The conjecture is solved, update memory
                    self.update_memory(
                        type='lemma',
                        content=conjecture,
                        correctness=judgement,
                        proof=proof,
                        comment=None if judgement == "true" else proof
                    )
                    break
                else:
                    judgement, proof = self.refine_proof(conjecture, judgement, proof, verification)

        if not solved:
            # The memory will also be updated if the conjecture is not solved
            self.update_memory(
                type='conjecture',
                content=conjecture,
                correctness='unknown',
                proof=None,
                comment=None
            )

        return None

    def explore_iteration_simplified(self, problem: str) -> Optional[bool]:
        """
        Explore for proof of this problem for one step (simplifier).
        It will propose a new goal, solve and verify it to update its memory.
        There is three possible return values of this function:
        1. true. The given problem is successfully solved.
        2. None. The given problem is not solved yet, and we need another iteration to finally solve this problem.
        """
        raw_exploration = self.explorer(problem, self.memory)

        # Extract, review, refine and collect new conjectures
        conjectures = extract_all_tag_content(raw_exploration, "conjecture")
        proofs = extract_all_tag_content(raw_exploration, "proof")
        if conjectures is not None and proofs is not None and len(conjectures) == len(proofs):
            for c, p in zip(conjectures, proofs):
                for _ in range(self.refine_iterations):
                    verification = self.pessimistic_eval(c, None, p)
                    if verification is None:
                        # The conjecture is solved, update memory
                        self.update_memory(
                            type='lemma',
                            content=c,
                            correctness=True,
                            proof=p,
                            comment=None
                        )
                        break
                    else:
                        nc, np = self.refine_proof_mas(c, p, verification)
                        if nc is not None:
                            c = nc
                        if np is not None:
                            p = np

        # Examine the final proof
        final_proof = extract_tag_content(raw_exploration, "final_proof")
        if final_proof is None:
            return None
        else:
            verification = self.pessimistic_eval(problem, None, final_proof)
            if verification is None:
                # The conjecture is solved, update memory
                self.update_memory(
                    type='theorem',
                    content=problem,
                    correctness=True,
                    proof=final_proof,
                    comment=None
                )
                return True
            else:
                return None

        return None

    def get_context(self, context_path: str):
        """
        This function will initialize the memory of this agent from existing path.
        We need to pass the path of the json file containing history explorations or newly constructed context
        """
        with open(context_path, "r", encoding="utf-8") as f:
            memory = json.load(f)
            self.memory += memory

    def save_logs(self, problem: str, correctness: Optional[bool]):
        os.makedirs(self.log_dir, exist_ok=True)
        main_log_path = self.log_dir + '/logs.json'
        memory_path = self.log_dir + '/memory.json'
        log_data = {
            'method': 'MathAgent',
            'problem': problem,
            'result': correctness,
            'memory_path': memory_path,
            'proof_model': self.proof_model,
            'eval_model': self.eval_model,
            'reform_model': self.reform_model,
            'max_steps': self.max_steps,
            'current_steps': self.current_steps,
            'reviews': self.reviews,
            'refine_iterations': self.refine_iterations,
            'parallel_solve_iterations': self.parallel_solve_iterations
        }
        if self.memory:
            log_data.update(**self.memory[-1])
        with open(main_log_path, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, ensure_ascii=False, indent=4)
        with open(memory_path, 'w', encoding='utf-8') as f:
            json.dump(self.memory, f, ensure_ascii=False, indent=4)

        logging.info(f"Saved logs to path {self.log_dir}")

        convert_memory_json_to_md(memory_path, self.log_dir + '/memory.md')
        convert_memory_json_to_latex(memory_path, self.log_dir + '/memory.tex')
        logging.info(f"Converted memory.json to markdown and saved to {self.log_dir}/memory.md")

    def __call__(self, problem: str) -> dict:
        """
        Directly calls this pipeline to solve a hard open problem.
        This function will return if it successfully solved the desired problem or it hits the maximum iteration cycles.
        """
        self.current_steps = 0
        correctness = None

        with tqdm(total=self.max_steps, desc="Exploring") as pbar:
            while (correctness is None and self.current_steps < self.max_steps):
                if self.current_steps % self.log_per_steps == 1:
                    self.save_logs(problem, correctness)

                self.current_steps += 1
                if self.method == "ma":
                    correctness = self.explore_iteration(problem)
                elif self.method == "mas":
                    correctness = self.explore_iteration_simplified(problem)

                pbar.update(1)

        self.save_logs(problem, correctness)

        return {
            "problem": problem,
            "correctness": correctness,
            "context": self.memory
        }
