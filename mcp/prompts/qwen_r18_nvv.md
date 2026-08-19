只根据可直接听见的声音输出 R18 非语言事件，不推断情绪、动机、关系、成人语义、身体状态或性唤起。

只输出一个 JSON 对象：
{"events":[{"start_ms":0,"end_ms":1000,"candidates":[{"label":"moan","confidence":0.8}],"perceptual":["breathy"],"pitch_relative":"similar","intensity":"soft","attack":"gradual","release":"fading"}],"trajectory":["moan"]}

label 只能是：moan、gasp、pant、breath、sigh、whimper、groan、nonlexical_vowel、laugh、cough、throat_clear、yawn、exercise_breathing、noise、speech。

perceptual 只能是：breathy、airy、soft、sharp、husky、rough、trembling、shaky、strained、suppressed、muffled、drawn_out、abrupt、wavering、rising_tail、falling_tail、fading、clipped、heavy_breathing、rapid_breathing、broken_breath。

pitch_relative 只能是 lower/similar/higher/clearly_higher；intensity 只能是 soft/medium/strong；attack 只能是 gradual/abrupt/none；release 只能是 fading/clipped/sustained/none。每段最多三个候选。

moan=持续有音高的非词汇元音；pant=连续快速重复吸呼循环；gasp=单次突然短促强吸气；sigh=较长呼气释放。不要把 heavy_breathing 放进 candidates；它只能放进 perceptual。没有明确事件时 events 为空。不要写 Markdown 或解释。
