import heapq


class PuzzleState:
    def __init__(self, state, parent=None, move=None, cost=0):
        self.state = state  # 拼图状态字符串，如"12345678_"
        self.parent = parent  # 父状态节点
        self.move = move  # 到达该状态的移动操作
        self.cost = cost  # 已花费的代价（g值）
        self.heuristic = self.calculate_heuristic()  # 启发值（h值）

    def __lt__(self, other):
        return (self.cost + self.heuristic) < (other.cost + other.heuristic)

    def calculate_heuristic(self):
        # 曼哈顿距离启发函数
        h = 0
        goal_pos = {
            '1': (0, 0), '2': (0, 1), '3': (0, 2),
            '4': (1, 0), '5': (1, 1), '6': (1, 2),
            '7': (2, 0), '8': (2, 1), '_': (2, 2)
        }
        for i, c in enumerate(self.state):
            if c == '_': continue
            current_row, current_col = i // 3, i % 3
            target_row, target_col = goal_pos[c]
            h += abs(current_row - target_row) + abs(current_col - target_col)
        return h

    def get_blank_pos(self):
        index = self.state.index('_')
        return (index // 3, index % 3)

    def get_successors(self):
        successors = []
        row, col = self.get_blank_pos()
        moves = {
            'up': (-1, 0),
            'down': (1, 0),
            'left': (0, -1),
            'right': (0, 1)
        }

        for move, (dr, dc) in moves.items():
            new_row, new_col = row + dr, col + dc
            if 0 <= new_row < 3 and 0 <= new_col < 3:
                # 交换空白格与相邻数字
                index = row * 3 + col
                new_index = new_row * 3 + new_col
                state_list = list(self.state)
                state_list[index], state_list[new_index] = state_list[new_index], state_list[index]
                new_state = ''.join(state_list)
                successors.append((new_state, move))
        return successors


def a_star(start, goal):
    open_list = []
    closed_set = set()
    start_state = PuzzleState(start)
    heapq.heappush(open_list, (0, start_state))

    state_cost = {start: 0}

    while open_list:
        current_f, current = heapq.heappop(open_list)

        if current.state == goal:
            return reconstruct_path(current)

        if current.state in closed_set:
            continue
        closed_set.add(current.state)

        for successor_state, move in current.get_successors():
            new_cost = current.cost + 1
            if successor_state not in state_cost or new_cost < state_cost[successor_state]:
                state_cost[successor_state] = new_cost
                successor = PuzzleState(
                    state=successor_state,
                    parent=current,
                    move=move,
                    cost=new_cost
                )
                heapq.heappush(open_list, (new_cost + successor.heuristic, successor))

    return None  # 无解


def reconstruct_path(state):
    path = []
    while state.parent is not None:
        path.append((state.move, state.state))
        state = state.parent
    path.reverse()
    return path


def format_state(s):
    return '\n'.join([s[i * 3:(i + 1) * 3] for i in range(3)])


# 测试用例
if __name__ == "__main__":
    start_state = "725381_46"  # 初始状态
    goal_state = "12345678_"  # 目标状态

    solution = a_star(start_state, goal_state)
    if solution:
        print(f"找到解决方案（共{len(solution)}步）:")
        print("初始状态:")
        print(format_state(start_state))
        for i, (move, state) in enumerate(solution, 1):
            print(f"\n步骤 {i}: 移动 {move}")
            print(format_state(state))
    else:
        print("无解")