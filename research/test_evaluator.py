import sys
sys.path.append(r"C:\Extra Programs\Files\AlcoSoft_Financial_Services")

from core.strategy import StrategySetEvaluator, CONDITION_REGISTRY, StrategyEvaluationContext

print("Evaluator:")
print(dir(StrategySetEvaluator))
print(StrategySetEvaluator.__init__.__code__.co_varnames)
print(StrategySetEvaluator.evaluate.__code__.co_varnames)
