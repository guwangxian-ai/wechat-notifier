// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"encoding/json"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"
)

func notifierFixture(t *testing.T) {
	t.Helper()
	oldStatus, oldDirectory, oldEnqueue, oldToken, oldKeyStatus := notifierStatusReader, notifierDirectoryReader, notifierEnqueue, config.OnebotToken, notifierKeyStatusReader
	t.Cleanup(func() {
		notifierStatusReader = oldStatus
		notifierDirectoryReader = oldDirectory
		notifierEnqueue = oldEnqueue
		config.OnebotToken = oldToken
		notifierKeyStatusReader = oldKeyStatus
	})
	config.OnebotToken = "test-token-only-not-a-real-secret"
	notifierReceipts.items = make(map[string]*notifierReceipt)
	notifierStatusReader = func() (map[string]any, error) {
		return map[string]any{
			"ready": true, "account_id": "wxid_test", "session_id": "session-test", "directory_enabled": true}, nil
	}
}

func TestNotifierDirectoryFailureIsActionableAndPrivate(t *testing.T) {
	notifierFixture(t)
	notifierDirectoryReader = func() (*ContactDirectory, error) { return nil, errors.New("通讯录不可用：key_missing") }
	notifierKeyStatusReader = func() (map[string]any, error) {
		return map[string]any{"installed": true, "candidates": float64(12), "validated": float64(0),
			"passphrase": false, "last_error": "private path and key", "keys": "secret-database-key"}, nil
	}
	r := httptest.NewRequest("GET", "/notifier/groups", nil)
	r.Header.Set("Authorization", "Bearer "+config.OnebotToken)
	w := httptest.NewRecorder()
	notifierGroupsHandler(w, r)
	if w.Code != 503 || !strings.Contains(w.Body.String(), `"reason":"key_missing"`) || !strings.Contains(w.Body.String(), `"candidates":12`) {
		t.Fatal(w.Code, w.Body.String())
	}
	for _, private := range []string{"private path", "secret-database-key", "last_error"} {
		if strings.Contains(w.Body.String(), private) {
			t.Fatal("private diagnostic exposed")
		}
	}
	notifierDirectoryReader = func() (*ContactDirectory, error) { return nil, errors.New("raw private exception") }
	w = httptest.NewRecorder()
	notifierGroupsHandler(w, r)
	if strings.Contains(w.Body.String(), "raw private exception") || !strings.Contains(w.Body.String(), `"reason":"unavailable"`) {
		t.Fatal(w.Body.String())
	}
}

func TestNotifierRequiresBearer(t *testing.T) {
	notifierFixture(t)
	r := httptest.NewRequest("GET", "/notifier/status", nil)
	w := httptest.NewRecorder()
	notifierStatusHandler(w, r)
	if w.Code != 401 {
		t.Fatal(w.Code)
	}
	r.Header.Set("Authorization", config.OnebotToken)
	w = httptest.NewRecorder()
	notifierStatusHandler(w, r)
	if w.Code != 401 {
		t.Fatal(w.Code)
	}
}

func TestNotifierGroupsExposeOnlyGroupIdentity(t *testing.T) {
	notifierFixture(t)
	notifierDirectoryReader = func() (*ContactDirectory, error) {
		return &ContactDirectory{OK: true,
			Users:  map[string]ContactEntry{"wxid_private": {Display: "private user"}},
			Keys:   map[string]ContactKey{"private": {Key: "never-export-this"}},
			Groups: map[string]ContactEntry{"123@chatroom": {Display: "测试群✅", Members: map[string]ContactEntry{"member": {Display: "private member"}}}}}, nil
	}
	r := httptest.NewRequest("GET", "/notifier/groups", nil)
	r.Header.Set("Authorization", "Bearer "+config.OnebotToken)
	w := httptest.NewRecorder()
	notifierGroupsHandler(w, r)
	if w.Code != 200 || !strings.Contains(w.Body.String(), "测试群✅") {
		t.Fatal(w.Code, w.Body.String())
	}
	for _, v := range []string{"never-export-this", "private user", "private member", "wxid_private"} {
		if strings.Contains(w.Body.String(), v) {
			t.Fatal("private directory data exposed")
		}
	}
}

func TestNotifierGroupsKeepCanonicalNameAlongsidePersonalDisplayName(t *testing.T) {
	notifierFixture(t)
	notifierDirectoryReader = func() (*ContactDirectory, error) {
		return &ContactDirectory{OK: true, Groups: map[string]ContactEntry{
			"123@chatroom": {Display: "运营通知测试群✅", Nickname: "项目运营通知测试群✅"},
		}}, nil
	}
	r := httptest.NewRequest("GET", "/notifier/groups", nil)
	r.Header.Set("Authorization", "Bearer "+config.OnebotToken)
	w := httptest.NewRecorder()
	notifierGroupsHandler(w, r)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"group_name":"项目运营通知测试群✅"`) || !strings.Contains(w.Body.String(), `"display_name":"运营通知测试群✅"`) {
		t.Fatal(w.Code, w.Body.String())
	}
}

func TestNotifierSubmitIsIdempotentAndOnlySubmitted(t *testing.T) {
	notifierFixture(t)
	calls := 0
	notifierEnqueue = func(m *SendMsg) error {
		calls++
		if m.GroupID != "123@chatroom" || m.Content != "text\n✅" {
			t.Fatal("wrong message")
		}
		return nil
	}
	request := notifierSubmission{RequestID: strings.Repeat("a", 32), GroupID: "123@chatroom", AccountID: "wxid_test", SessionID: "session-test", Text: "text\n✅"}
	encoded, _ := json.Marshal(request)
	for i := 0; i < 2; i++ {
		r := httptest.NewRequest("POST", "/notifier/submit", strings.NewReader(string(encoded)))
		r.Header.Set("Authorization", "Bearer "+config.OnebotToken)
		w := httptest.NewRecorder()
		notifierSubmitHandler(w, r)
		if w.Code != 200 || !strings.Contains(w.Body.String(), `"receipt":"submitted"`) {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	if calls != 1 {
		t.Fatal(calls)
	}
	request.Text = "different"
	encoded, _ = json.Marshal(request)
	r := httptest.NewRequest("POST", "/notifier/submit", strings.NewReader(string(encoded)))
	r.Header.Set("Authorization", "Bearer "+config.OnebotToken)
	w := httptest.NewRecorder()
	notifierSubmitHandler(w, r)
	if w.Code != 409 || calls != 1 {
		t.Fatal(w.Code, calls)
	}
}

func TestNotifierRejectsAccountAndSessionChange(t *testing.T) {
	notifierFixture(t)
	notifierEnqueue = func(*SendMsg) error { t.Fatal("must not send"); return nil }
	for _, pair := range [][2]string{{"other", "session-test"}, {"wxid_test", "stale-session"}} {
		x := notifierSubmission{RequestID: strings.Repeat("b", 32), GroupID: "123@chatroom", AccountID: pair[0], SessionID: pair[1], Text: "test"}
		b, _ := json.Marshal(x)
		r := httptest.NewRequest("POST", "/notifier/submit", strings.NewReader(string(b)))
		r.Header.Set("Authorization", "Bearer "+config.OnebotToken)
		w := httptest.NewRecorder()
		notifierSubmitHandler(w, r)
		if w.Code != 409 {
			t.Fatal(w.Code)
		}
	}
}
