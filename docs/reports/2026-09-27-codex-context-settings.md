# Codex 컨텍스트 설정 확인

확인일: 2026-09-27. 설정 변경 없이 읽기만 수행함.

## 글로벌 설정
출처: `/Users/ssojux2/.codex/config.toml`.

| 항목 | 설정값 |
|---|---:|
| 기본 모델 | gpt-6-sol |
| 기본 reasoning effort | high |
| model_context_window | 872000 tokens |
| model_auto_compact_token_limit | 800000 tokens |
| model_auto_compact_token_limit_scope | total |

프로젝트의 `.codex/config.toml`, `[profiles]`의 별도 값, `*.config.toml` 프로필 파일은 발견되지 않았다. 현재 확인 범위에서는 모델별 별도 context override 대신 글로벌 값이 적용된다.

## 실제 실행 기록
- 현재 부모 세션 gpt-6-sol: token_count 이벤트의 model_context_window=828400.
- 현재 Sol worker gpt-6-sol: 같은 값 828400.
- 모델 교체 전 gpt-5.6-sol worker: 같은 값 828400.
- 828400은 설정값 872000의 95%와 일치한다. 모델 캐시의 effective_context_window_percent도 95다.
- 자동 compact 800000은 설정 파일에서 확인한 값이다. 이 문서는 실제 발동 시점이나 CLI 내부 clamp를 측정한 결과가 아니다.

## 모델 캐시의 기본값
출처: `/Users/ssojux2/.codex/models_cache.json`.
- gpt-6-astra, gpt-6-sol, gpt-6-luna, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna 모두 context_window=272000, effective_context_window_percent=95로 기록됨.
- 이 값은 로컬 Codex 모델 메타데이터의 기본값이다. 공식 API 최대 지원 길이나 현재 글로벌 override와 혼동하지 않는다.
- 현재 실행 기록은 기본값의 95%=258400이 아니라 글로벌 override의 95%=828400을 보고한다.

## 공식 설정 의미
`model_context_window`는 활성 모델에 사용할 컨텍스트 길이, `model_auto_compact_token_limit`는 자동 압축 기준 설정이다. `total`은 활성 컨텍스트 전체를 계산한다.
출처: https://learn.chatgpt.com/docs/config-file/config-reference
